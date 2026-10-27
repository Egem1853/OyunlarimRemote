"""
Steam Uzaktan Başlatıcı (v2)
----------------------------
PC'de çalıştır. iPhone'dan Safari ile adresi aç.
- Oyun seç -> PC'de Steam ile başlar
- Çalışan oyunu uzaktan kapat
- Başka oyuna dokunursan çalışan oyun kapatılıp yenisi açılır
- Ayarlar sekmesi (tema/özelleştirme, planlayıcı, programlar, PIN), pano köprüsü, klavye kısayolları, uzun basınca not/gizle/mağaza, mola hatırlatıcısı, günlük hedef, son oturumlar, şeritler, medya tuşları, indirme takibi, uzaktan yazı/kaydırma, görünüm ayarları, haftalık oynama grafiği, oyun zamanlayıcısı, PC'ye mesaj, iPhone Kestirmeler/widget bağlantıları, canlı ekran izleme + dokunarak kontrol, Steam profil sayfası, favoriler, PC durumu (CPU/RAM/GPU), ses, ekran görüntüsü, güç menüsü
- Tailscale ile evin dışından da çalışır

Gereksinim: Windows + Python 3.8+ (ek kütüphane gerekmez)
Çalıştırma:  python steam_remote.py
"""
import ctypes
import glob
import random
import secrets
import struct
import zlib
import hmac
import array
import hashlib
import json
import sqlite3
import ssl
import shutil
import mimetypes
import os
import re
import socket
import sys
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import base64
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import winreg
except ImportError:
    winreg = None

PORT = 8765
STANDALONE = True  # genel (bağımsız) sürüm: admin / merkez sunucu / arkadaş ağı kapalı, gizli anahtar yok
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
PIN = "1234"  # Genel sürüm varsayılanı: ilk açılışta Ayarlar > PIN'den kendi PIN'ini belirle
ADMIN_CODE = ""  # genel sürümde merkez/yönetici yok
ADMIN_N = 0
ADMIN_D = 0  # genel sürümde yönetici yok
STEAM_API_KEY = ""  # isteğe bağlı: profil istatistikleri için (steamcommunity.com/dev/apikey)

NO_WINDOW = 0x08000000  # pythonw ile çalışırken konsol penceresi açılmasın
SKIP_IDS = {"228980", "1070560", "1391110", "1628350", "1493710"}
SKIP_NAMES = ("Steamworks", "Steam Linux Runtime", "Proton", "Steam Controller")

LOG = os.path.join(_BASE, "steam_remote.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass



# ---------- Steam ----------
def find_steam():
    if winreg:
        for hive, key, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    return os.path.normpath(winreg.QueryValueEx(k, val)[0])
            except OSError:
                pass
    default = r"C:\Program Files (x86)\Steam"
    return default if os.path.isdir(default) else None


def library_dirs(steam):
    libs = {os.path.join(steam, "steamapps")}
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="ignore") as f:
            for p in re.findall(r'"path"\s+"([^"]+)"', f.read()):
                libs.add(os.path.join(p.replace("\\\\", "\\"), "steamapps"))
    except OSError:
        pass
    return [l for l in libs if os.path.isdir(l)]


_cache = {"t": 0, "games": []}


def read_games(force=False):
    if not force and time.time() - _cache["t"] < 30:
        return _cache["games"]
    steam = find_steam()
    games = {}
    if steam:
        for lib in library_dirs(steam):
            for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
                try:
                    with open(acf, encoding="utf-8", errors="ignore") as f:
                        t = f.read()
                    appid = re.search(r'"appid"\s+"(\d+)"', t).group(1)
                    name = re.search(r'"name"\s+"([^"]*)"', t).group(1)
                    inst = re.search(r'"installdir"\s+"([^"]*)"', t)
                    last = re.search(r'"LastPlayed"\s+"(\d+)"', t)
                    size = re.search(r'"SizeOnDisk"\s+"(\d+)"', t)
                    stt = re.search(r'"StateFlags"\s+"(\d+)"', t)
                    btd = re.search(r'"BytesToDownload"\s+"(\d+)"', t)
                    bdl = re.search(r'"BytesDownloaded"\s+"(\d+)"', t)
                except (AttributeError, OSError):
                    continue
                if appid in SKIP_IDS or name.startswith(SKIP_NAMES):
                    continue
                games[appid] = {
                    "appid": appid,
                    "name": name,
                    "last": int(last.group(1)) if last else 0,
                    "size": int(size.group(1)) if size else 0,
                    "dl": [int(btd.group(1)), int(bdl.group(1))] if btd and bdl and stt and not int(stt.group(1)) & 4 and int(btd.group(1)) > 0 else None,
                    "dir": os.path.join(lib, "common", inst.group(1)) if inst else "",
                    "flags": int(stt.group(1)) if stt else 0,
                    "lib": lib,
                }
    _cache["games"] = sorted(games.values(), key=lambda g: (-g["last"], g["name"].lower()))
    _cache["t"] = time.time()
    return _cache["games"]


def find_game(appid):
    return next((g for g in read_games() if g["appid"] == str(appid)), None)


def running_appid():
    """Steam'in şu an çalışan oyun olarak bildirdiği appid (yoksa '0')."""
    if not winreg:
        return "0"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return str(winreg.QueryValueEx(k, "RunningAppID")[0])
    except OSError:
        return "0"


# ---------- Süreç yönetimi ----------
def processes():
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=30,
                             creationflags=NO_WINDOW).stdout
        out = raw.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
        data = json.loads(out)
    except Exception as e:
        log(f"processes() hata: {e}")
        return []
    return [data] if isinstance(data, dict) else data


def game_pids(game):
    """(oyun klasöründeki süreçler, Steam'in 'reaper' başlatıcı süreçleri)"""
    root = game["dir"].lower().rstrip("\\/")
    me = os.getpid()
    pat = re.compile(r"steamlaunch\s+appid=%s(\s|$)" % re.escape(game["appid"]))
    found, launchers = set(), set()
    for p in processes():
        pid = p.get("ProcessId")
        if pid is None or pid == me:
            continue
        exe = (p.get("ExecutablePath") or "").lower()
        cmdl = (p.get("CommandLine") or "").lower()
        if root and exe.startswith(root + os.sep):
            found.add(pid)
        if pat.search(cmdl):
            launchers.add(pid)
    return sorted(found), sorted(launchers)


def kill(pids, force):
    for pid in pids:
        args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        r = subprocess.run(args, capture_output=True, text=True, errors="ignore",
                           creationflags=NO_WINDOW)
        log(f"taskkill pid={pid} force={force}: {(r.stdout + r.stderr).strip()[:200]}")


def stop_game(appid):
    """Önce nazikçe kapatmayı dener, kapanmazsa zorla sonlandırır."""
    game = find_game(appid)
    if not game:
        log(f"stop_game: {appid} kütüphanede yok")
        return False
    _state["stop_req"] = time.time()
    pids, launchers = game_pids(game)
    log(f"{game['name']} kapatılıyor. klasör={game['dir']} süreçler={pids} başlatıcı={launchers}")
    if not pids and not launchers:
        log("UYARI: oyunun hiçbir süreci bulunamadı.")
    kill(pids, force=False)
    for _ in range(5):
        time.sleep(1)
        pids, launchers = game_pids(game)
        if not pids and not launchers:
            log("oyun normal yoldan kapandı")
            return True
    kill(pids + launchers, force=True)
    time.sleep(1)
    return True


def wait_until_closed(timeout=10):
    for _ in range(timeout):
        if running_appid() == "0":
            return True
        time.sleep(1)
    return running_appid() == "0"


# ---------- Ağ ----------
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                             timeout=5, creationflags=NO_WINDOW).stdout.strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


# ---------- PC kontrolü ----------
DATA = os.path.join(_BASE, "steam_remote_data.json")
_lock = threading.Lock()
_state = {"cpu": 0, "since": 0, "app": "0", "kill_at": 0, "last_break": 0, "goal_t": 0, "goal_day": ""}


def _data():
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f)


def load_favs():
    return set(_data().get("favs", []))


def set_fav(appid, on):
    with _lock:
        d = _data()
        favs = set(d.get("favs", []))
        (favs.add if on else favs.discard)(appid)
        d["favs"] = sorted(favs)
        _save(d)


def log_session(appid, start, end):
    if end - start < 60:
        return
    with _lock:
        d = _data()
        d["sessions"] = (d.get("sessions", []) + [[appid, int(start), int(end)]])[-6000:]
        _save(d)


def activity():
    """Son 7 günün oynama süreleri (bu programın çalıştığı süre boyunca tutulur)."""
    now = time.time()
    sess = [tuple(x) for x in _data().get("sessions", [])]
    if _state["app"] != "0" and _state["since"]:
        sess.append((_state["app"], _state["since"], now))
    lt = time.localtime(now)
    mid = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    days, per = [], {}
    for i in range(6, -1, -1):
        a, b = mid - i * 86400, mid - i * 86400 + 86400
        tot = 0
        for appid, st, en in sess:
            o = max(0, min(en, b) - max(st, a))
            tot += o
            if o:
                per[appid] = per.get(appid, 0) + o
        days.append({"t": int(a) + 43200, "min": round(tot / 60)})
    recent = []
    for appid, st, en in sorted(sess, key=lambda x: -x[1])[:8]:
        g = find_game(appid)
        recent.append({"appid": appid, "name": g["name"] if g else appid, "t": int(st), "min": max(1, round((en - st) / 60))})
    streak = 0
    for i in range(60):
        a = mid - i * 86400
        if any(min(en, a + 86400) - max(st, a) > 0 for _, st, en in sess):
            streak += 1
        elif i > 0:
            break
    top = []
    for appid, sec in sorted(per.items(), key=lambda x: -x[1])[:5]:
        g = find_game(appid)
        top.append({"appid": appid, "name": g["name"] if g else appid, "min": round(sec / 60)})
    return {"recent": recent, "days": days, "streak": streak, "total": sum(d["min"] for d in days), "top": [t for t in top if t["min"]]}


def show_message(text):
    threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
        0, text[:300], "Telefondan mesaj", 0x40 | 0x40000), daemon=True).start()


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


class _MEM(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint32), ("load", ctypes.c_uint32),
                ("total", ctypes.c_uint64), ("avail", ctypes.c_uint64),
                ("x", ctypes.c_uint64 * 5)]


def _cpu_times():
    i, k, u = _FT(), _FT(), _FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    v = lambda f: (f.hi << 32) | f.lo
    return v(i), v(k) + v(u)


def monitor():
    """Arka planda CPU yükünü ve oyun süresini takip eder."""
    prev = _cpu_times()
    tick = 0
    while True:
        time.sleep(3)
        tick += 1
        try:
            if tick % 5 == 1:
                check_downloads()
            if tick % 7200 == 20:
                threading.Thread(target=lambda: wishlist(force=True), daemon=True).start()
        except Exception as e:
            log(f"indirme takibi: {e}")
        try:
            cur = _cpu_times()
            di, dt = cur[0] - prev[0], cur[1] - prev[1]
            prev = cur
            _state["cpu"] = round(100 * (dt - di) / dt) if dt else 0
            app = running_appid()
            earn_tick(app != "0")
            if tick % 20 == 7:
                coplay_tick()
            if app != "0" and (now2 := time.time()) - _state.get("pres_hb", 0) > 1500:
                _state["pres_hb"] = now2
                threading.Thread(target=chat_presence, daemon=True).start()
            check_schedules()
            if app != _state["app"]:
                if _state["app"] != "0" and _state["since"]:
                    log_session(_state["app"], _state["since"], time.time())
                threading.Thread(target=on_game_change, args=(_state["app"], _state["since"], app), daemon=True).start()
                _state["app"], _state["since"], _state["kill_at"] = app, time.time(), 0
                _state["last_break"] = time.time()
            if app != "0":  # mola hatırlatıcısı ve günlük hedef
                d, now = _data(), time.time()
                if d.get("break") and now - _state["last_break"] >= d["break"] * 60:
                    _state["last_break"] = now
                    show_message(f"Mola zamanı! {int((now - _state['since']) / 60)} dakikadır oynuyorsun.")
                    notify("brk", "Mola zamanı", f"{int((now - _state['since']) / 60)} dakikadır oynuyorsun.")
                if d.get("goal") and now - _state["goal_t"] > 60:
                    _state["goal_t"] = now
                    today = activity()["days"][-1]["min"]
                    if today >= d["goal"] and _state["goal_day"] != time.strftime("%Y%m%d"):
                        _state["goal_day"] = time.strftime("%Y%m%d")
                        show_message(f"Günlük hedefine ulaştın ({today} dk). Bugünlük yeter!")
                        notify("goal", "Günlük hedef", f"Bugün {today} dk oynadın, hedefine ulaştın.")
            if _state["kill_at"] and time.time() >= _state["kill_at"]:
                _state["kill_at"] = 0
                if app != "0":
                    log("oyun zamanlayıcısı doldu, oyun kapatılıyor")
                    g = find_game(app)
                    stop_game(app)
                    notify("timer", "Oyun zamanlayıcısı", f"{g['name'] if g else 'Oyun'} süre dolduğu için kapatıldı.", app)
        except Exception as e:
            log(f"monitor: {e}")


def uptime_min():
    k = ctypes.windll.kernel32
    k.GetTickCount64.restype = ctypes.c_ulonglong
    return int(k.GetTickCount64() // 60000)


def downloads():
    return [{"appid": g["appid"], "name": g["name"], "pct": round(100 * g["dl"][1] / g["dl"][0])}
            for g in read_games(force=True) if g.get("dl")]


def system_info():
    m = _MEM()
    m.len = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    gpu = None
    try:
        o = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW).stdout.split(",")
        gpu = [int(x) for x in o[:4]]
    except Exception:
        pass
    return {"cpu": _state["cpu"], "ram": m.load, "media": media_info(),
            "ram_used": round((m.total - m.avail) / 2**30, 1), "ram_total": round(m.total / 2**30),
            "gpu": gpu, "uptime": uptime_min(), "dl": downloads()}


def press_key(vk, n=1):
    u = ctypes.windll.user32
    for _ in range(n):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, 2, 0)


VOLUME = {"up": (0xAF, 5), "down": (0xAE, 5), "mute": (0xAD, 1),
          "playpause": (0xB3, 1), "next": (0xB0, 1), "prev": (0xB1, 1)}
STEAM_URLS = {"bigpicture": "steam://open/bigpicture", "store": "steam://store", "friends": "steam://open/friends",
              "downloads": "steam://open/downloads", "library": "steam://open/games", "exit": "steam://exit"}
POWER = {
    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    "restart": ["shutdown", "/r", "/t", "3"],
    "shutdown": ["shutdown", "/s", "/t", "3"],
}

def png_bytes(w, h, raw):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 3)) + chunk(b"IEND", b""))


def screenshot(width=1100):
    """Ana ekranın küçültülmüş PNG görüntüsü (ek kütüphane gerektirmez)."""
    try:
        from ctypes import c_void_p as P, c_int as I
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.SetProcessDPIAware()
        u.GetDC.restype = g.CreateCompatibleDC.restype = g.CreateCompatibleBitmap.restype = P
        g.SelectObject.restype = P
        u.GetDC.argtypes = [P]
        u.ReleaseDC.argtypes = [P, P]
        g.CreateCompatibleDC.argtypes = [P]
        g.CreateCompatibleBitmap.argtypes = [P, I, I]
        g.SelectObject.argtypes = [P, P]
        g.SetStretchBltMode.argtypes = [P, I]
        g.StretchBlt.argtypes = [P, I, I, I, I, P, I, I, I, I, ctypes.c_uint32]
        g.GetDIBits.argtypes = [P, P, ctypes.c_uint, ctypes.c_uint, P, P, ctypes.c_uint]
        g.DeleteObject.argtypes = [P]
        g.DeleteDC.argtypes = [P]
        W, H = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        w = min(width, W)
        h = H * w // W
        sdc = u.GetDC(None)
        mdc = g.CreateCompatibleDC(sdc)
        bmp = g.CreateCompatibleBitmap(sdc, w, h)
        g.SelectObject(mdc, bmp)
        g.SetStretchBltMode(mdc, 4)  # HALFTONE: düzgün küçültme
        g.StretchBlt(mdc, 0, 0, w, h, sdc, 0, 0, W, H, 0x00CC0020)
        stride = (w * 3 + 3) & ~3
        buf = ctypes.create_string_buffer(stride * h)
        info = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 24, 0, 0, 0, 0, 0, 0), 44)
        g.GetDIBits(mdc, bmp, 0, h, buf, info, 0)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(None, sdc)
        data = buf.raw
        try:
            from PIL import Image
            import io
            out = io.BytesIO()
            Image.frombuffer("RGB", (w, h), data, "raw", "BGR", stride, 1).save(out, "JPEG", quality=60)
            return out.getvalue(), "image/jpeg"
        except ImportError:
            pass
        raw = bytearray()
        for y in range(h):
            row = bytearray(data[y * stride:y * stride + w * 3])
            row[0::3], row[2::3] = row[2::3], row[0::3]  # BGR -> RGB
            raw += b"\x00" + row
        return png_bytes(w, h, raw), "image/png"
    except Exception as e:
        log(f"ekran görüntüsü hata: {e!r}")
        return None


class _KI(ctypes.Structure):
    _fields_ = [("vk", ctypes.c_ushort), ("scan", ctypes.c_ushort), ("flags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("extra", ctypes.c_void_p)]


class _IU(ctypes.Union):
    _fields_ = [("ki", _KI), ("pad", ctypes.c_ubyte * 32)]


class _IN(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _IU)]


def type_text(text):
    """Metni PC'de odaktaki pencereye yazar (Türkçe karakterler dahil)."""
    try:
        raw = text.encode("utf-16-le")
        arr = []
        for c in struct.unpack("<%dH" % (len(raw) // 2), raw):
            for fl in (4, 6):  # KEYEVENTF_UNICODE, sonra KEYUP
                arr.append(_IN(1, _IU(ki=_KI(0, c, fl, 0, None))))
        ctypes.windll.user32.SendInput(len(arr), (_IN * len(arr))(*arr), ctypes.sizeof(_IN))
    except Exception as e:
        log(f"yazı gönderme hatası: {e!r}")


def scroll(d):
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, (360 * d) & 0xFFFFFFFF, 0)


def click_at(x, y, right=False):
    u = ctypes.windll.user32
    W, H = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    u.SetCursorPos(int(x * W), int(y * H))
    down, up = (8, 16) if right else (2, 4)
    u.mouse_event(down, 0, 0, 0, 0)
    u.mouse_event(up, 0, 0, 0, 0)


KEYS = {"backspace": 0x08, "enter": 0x0D, "esc": 0x1B, "space": 0x20, "tab": 0x09,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}
_icon = {}


def icon_png(n=180):
    """Ana ekran simgesi: lacivert zemin, mavi daire, beyaz oynat üçgeni."""
    if n not in _icon:
        rows, c = bytearray(), n / 2
        lx, rx = c - 0.12 * n, c + 0.16 * n
        for y in range(n):
            rows += b"\x00"
            t = y / n
            for x in range(n):
                px = (int(27 + 10 * t), int(40 + 25 * t), int(56 + 50 * t))
                if (x - c) ** 2 + (y - c) ** 2 < (0.34 * n) ** 2:
                    px = (102, 192, 244)
                    if lx < x < rx and abs(y - c) < (rx - x) * 0.72:
                        px = (27, 40, 56)
                rows += bytes(px)
        _icon[n] = png_bytes(n, n, rows)
    return _icon[n]


def shutdown_timer(minutes):
    subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)  # eskisini iptal et
    subprocess.Popen(["shutdown", "/s", "/t", str(minutes * 60)], creationflags=NO_WINDOW)
    log(f"{minutes} dk sonra kapatma planlandı")


def cancel_shutdown():
    subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
    log("planlı kapatma iptal edildi")


def launch_game(appid):
    if not find_game(appid):
        return False
    cur = running_appid()
    if cur not in ("0", appid):
        stop_game(cur)
        wait_until_closed(10)
    os.startfile(f"steam://rungameid/{appid}")
    return True


def quick_command(q):
    """/q?pin=..&do=.. bağlantıları (iPhone Kestirmeler widget'ı için)."""
    do = q.get("do", "")
    if do == "launch":
        return "Başlatılıyor" if launch_game(q.get("id", "")) else "Oyun bulunamadı"
    if do == "stop":
        cur = running_appid()
        if cur != "0":
            stop_game(cur)
        return "Oyun kapatıldı"
    if do in VOLUME:
        press_key(*VOLUME[do])
        return "Tamam"
    if do == "bigpicture":
        os.startfile("steam://open/bigpicture")
        return "Big Picture açılıyor"
    if do == "youtube":
        term = str(q.get("q", "")).strip()[:100]
        os.startfile("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(term) if term else "https://www.youtube.com")
        return "YouTube açılıyor" + (f": {term}" if term else "")
    if do == "timer":
        try:
            m = max(1, min(720, int(q.get("m") or 30)))
        except ValueError:
            return "Geçersiz süre"
        shutdown_timer(m)
        return f"PC {m} dk sonra kapanacak"
    if do == "cancel":
        cancel_shutdown()
        return "Planlı kapatma iptal edildi"
    if do in ("last", "random"):
        gs = read_games()
        pool = [g for g in gs if g["last"]][:1] if do == "last" else gs
        if not pool:
            return "Oyun bulunamadı"
        g = random.choice(pool)
        launch_game(g["appid"])
        return "Başlatılıyor: " + g["name"]
    if do in ("lock", "sleep", "shutdown"):
        threading.Timer(1.0, lambda: subprocess.Popen(POWER[do], creationflags=NO_WINDOW)).start()
        return "Komut gönderildi"
    return "Bilinmeyen komut"


COMBOS = {"alttab": [0x12, 0x09], "altf4": [0x12, 0x73], "win": [0x5B], "windesk": [0x5B, 0x44],
          "f11": [0x7A], "taskmgr": [0x11, 0x10, 0x1B]}


def press_combo(vks):
    u = ctypes.windll.user32
    for v in vks:
        u.keybd_event(v, 0, 0, 0)
    for v in reversed(vks):
        u.keybd_event(v, 0, 2, 0)


def get_pin():
    return str(_data().get("pin") or PIN)


def get_lang():
    l = _data().get("lang", "tr")
    return l if l in ("tr", "en") else "tr"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _clip():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = k.GlobalLock.restype = u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    return u, k


def set_clipboard(text):
    u, k = _clip()
    data = (text + "\0").encode("utf-16-le")
    h = k.GlobalAlloc(0x42, len(data))
    ctypes.memmove(k.GlobalLock(h), data, len(data))
    k.GlobalUnlock(h)
    if u.OpenClipboard(None):
        u.EmptyClipboard()
        u.SetClipboardData(13, h)
        u.CloseClipboard()


def get_clipboard():
    u, k = _clip()
    text = ""
    if u.OpenClipboard(None):
        h = u.GetClipboardData(13)
        if h:
            text = ctypes.wstring_at(k.GlobalLock(h))
            k.GlobalUnlock(h)
        u.CloseClipboard()
    return text[:2000]


def check_schedules():
    hm, today, due = time.strftime("%H:%M"), time.strftime("%Y%m%d"), []
    with _lock:
        d = _data()
        for sc in d.get("schedules", []):
            if sc["time"] == hm and sc.get("done") != today:
                sc["done"] = today
                due.append(sc["appid"])
        if due:
            _save(d)
    for appid in due:
        log(f"planlı oyun başlatılıyor: {appid}")
        g = find_game(appid)
        if launch_game(appid):
            notify("sched", "Planlı oyun açılıyor", f"{g['name'] if g else appid} PC'de başlatılıyor.", appid)


# ---------- Profil ----------
_web = {"t": 0, "data": None}


def api_key():
    return str(_data().get("apikey") or STEAM_API_KEY)


def steam_api(path, **q):
    q["key"] = api_key()
    url = "https://api.steampowered.com/" + path + "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def web_data(sid):
    if not api_key() or not sid:
        return None
    if _web["data"] and _web.get("sid") == sid and time.time() - _web["t"] < 600:
        return _web["data"]
    d = {}

    def part(name, fn):
        try:
            d.update(fn())
        except Exception as e:
            log(f"steam api ({name}): {e}")

    def summary():
        p = steam_api("ISteamUser/GetPlayerSummaries/v2/", steamids=sid)["response"]["players"][0]
        return {"state": p.get("personastate", 0), "created": p.get("timecreated", 0)}

    def badges():
        b = steam_api("IPlayerService/GetBadges/v1/", steamid=sid)["response"]
        return {"level": b.get("player_level", 0), "xp": b.get("player_xp", 0),
                "xp_next": b.get("player_xp_needed_to_level_up", 0), "badges": len(b.get("badges", []))}

    def friends():
        f = steam_api("ISteamUser/GetFriendList/v1/", steamid=sid, relationship="friend")
        return {"friends": len(f["friendslist"]["friends"])}

    def owned():
        o = steam_api("IPlayerService/GetOwnedGames/v1/", steamid=sid, include_appinfo=1,
                      include_played_free_games=1)["response"]
        gs = o.get("games", [])
        row = lambda g, k: {"appid": str(g["appid"]), "name": g.get("name", ""), "hours": round(g[k] / 60, 1) if k == "playtime_2weeks" else round(g[k] / 60)}
        top = sorted(gs, key=lambda g: -g.get("playtime_forever", 0))[:5]
        rec = sorted(gs, key=lambda g: -g.get("playtime_2weeks", 0))[:3]
        return {"games": o.get("game_count", len(gs)),
                "hours": round(sum(g.get("playtime_forever", 0) for g in gs) / 60),
                "hours_2w": round(sum(g.get("playtime_2weeks", 0) for g in gs) / 60, 1),
                "top": [row(g, "playtime_forever") for g in top if g.get("playtime_forever")],
                "recent": [row(g, "playtime_2weeks") for g in rec if g.get("playtime_2weeks")]}

    for n, fn in (("summary", summary), ("badges", badges), ("friends", friends), ("owned", owned)):
        part(n, fn)
    _web.update(t=time.time(), data=d or None, sid=sid)
    return d or None


def steam_profile():
    steam = find_steam()
    sid = name = acct = avatar = ""
    try:
        t = open(os.path.join(steam, "config", "loginusers.vdf"), encoding="utf-8", errors="ignore").read()
        users = re.findall(r'"(\d{17})"\s*\{(.*?)\}', t, re.S)
        pick = next((u for u in users if re.search(r'"MostRecent"\s+"1"', u[1])), users[0] if users else None)
        if pick:
            sid = pick[0]
            get = lambda k: (lambda m: m.group(1) if m else "")(re.search(r'"%s"\s+"([^"]*)"' % k, pick[1]))
            name, acct = get("PersonaName"), get("AccountName")
        with open(os.path.join(steam, "config", "avatarcache", sid + ".png"), "rb") as f:
            avatar = base64.b64encode(f.read()).decode()
    except Exception as e:
        log(f"profil okunamadı: {e}")
    games = read_games()
    return {"name": name or acct or "Steam kullanıcısı", "account": acct, "steamid": sid, "avatar": avatar,
            "installed": len(games), "size_gb": round(sum(g["size"] for g in games) / 2**30),
            "favs": len(load_favs()), "has_key": bool(api_key()), "web": web_data(sid)}



def login_user():
    """(steamid, görünen ad) — Steam'e en son giriş yapan hesap."""
    try:
        t = open(os.path.join(find_steam(), "config", "loginusers.vdf"), encoding="utf-8", errors="ignore").read()
        users = re.findall(r'"(\d{17})"\s*\{(.*?)\}', t, re.S)
        pick = next((u for u in users if re.search(r'"MostRecent"\s+"1"', u[1])), users[0] if users else None)
        if pick:
            m = re.search(r'"PersonaName"\s+"([^"]*)"', pick[1]) or re.search(r'"AccountName"\s+"([^"]*)"', pick[1])
            return pick[0], (m.group(1) if m else "")
    except Exception:
        pass
    return "", ""


def persona():
    return login_user()[1] or "Oyuncu"


# ---------- Arkadaşlar ----------
_fr = {"t": 0, "data": None}


def friends_status():
    sid = login_user()[0]
    if not api_key() or not sid:
        return {"error": "key"}
    if _fr["data"] and time.time() - _fr["t"] < 45:
        return _fr["data"]
    try:
        fl = steam_api("ISteamUser/GetFriendList/v1/", steamid=sid, relationship="friend")["friendslist"]["friends"]
    except urllib.error.HTTPError as e:
        log(f"arkadaş listesi: {e}")
        return {"error": "private" if e.code in (401, 403) else "net"}
    except Exception as e:
        log(f"arkadaş listesi: {e}")
        return {"error": "net"}
    ids, players = [f["steamid"] for f in fl], []
    try:
        for i in range(0, len(ids), 100):
            players += steam_api("ISteamUser/GetPlayerSummaries/v2/", steamids=",".join(ids[i:i + 100]))["response"]["players"]
    except Exception as e:
        log(f"arkadaş durumları: {e}")
        return {"error": "net"}
    out = [{"id": p["steamid"], "name": p.get("personaname", ""), "av": p.get("avatarmedium", ""),
            "st": p.get("personastate", 0), "game": p.get("gameextrainfo", ""), "gid": str(p.get("gameid", "")),
            "off": p.get("lastlogoff", 0)} for p in players]
    out.sort(key=lambda x: (not x["game"], not x["st"], -x["off"] if not x["st"] else 0, x["name"].lower()))
    _fr.update(t=time.time(), data={"friends": out})
    return _fr["data"]


# ---------- Bildirimler (ntfy) ve Discord ----------
NT_DEFAULT = {"chat": True, "sale": True, "dl": True, "crash": True, "sched": True, "timer": True, "brk": False, "goal": False, "game": False,
              "dc_start": True, "dc_end": False, "dc_copy": False}
WEBHOOK_RE = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")


def nt_settings(d=None):
    return {**NT_DEFAULT, **(d if d is not None else _data()).get("nt", {})}


def header_url(appid):
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


def ntfy_send(d, title, msg, appid=None):
    body = {"topic": d["ntfy_topic"], "title": title, "message": msg, "tags": ["video_game"]}
    if _state.get("url"):
        body["click"] = _state["url"]
    if appid:
        body["attach"] = header_url(appid)
        body["filename"] = "kapak.jpg"
    req = urllib.request.Request(str(d.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def discord_send(url, content, appid=None, name=""):
    payload = {"username": "Steam Uzaktan", "content": content[:1900]}
    if appid:
        payload["embeds"] = [{"title": name or appid, "url": f"https://store.steampowered.com/app/{appid}",
                              "color": 0x66C0F4, "image": {"url": header_url(appid)}}]
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (steam_remote, 1.0)"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def notify(kind, title, msg, appid=None):
    d = _data()
    nt = nt_settings(d)
    if not nt.get(kind):
        return

    def run():
        if d.get("ntfy_topic"):
            try:
                ntfy_send(d, title, msg, appid)
            except Exception as e:
                log(f"ntfy hata: {e}")
        if nt.get("dc_copy") and d.get("webhook"):
            try:
                discord_send(d["webhook"], f"**{title}**\n{msg}")
            except Exception as e:
                log(f"discord hata: {e}")
    threading.Thread(target=run, daemon=True).start()


def discord_auto(kind, content, appid=None, name=""):
    d = _data()
    if d.get("webhook") and nt_settings(d).get(kind):
        try:
            discord_send(d["webhook"], content, appid, name)
        except Exception as e:
            log(f"discord hata: {e}")


def crashed(game):
    """Oyun kapandıktan sonra Windows olay günlüğünde o oyunun klasöründen bir çökme kaydı var mı?"""
    root = (game.get("dir") or "").lower().rstrip("\\/")
    if not root:
        return False
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000,1002;StartTime=(Get-Date).AddMinutes(-2)} "
          "-ErrorAction SilentlyContinue | ForEach-Object { $_.Message }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=25,
                             creationflags=NO_WINDOW).stdout.decode("utf-8", "ignore").lower()
    except Exception as e:
        log(f"çökme kontrolü: {e}")
        return False
    return root in out


def fmt_min(m):
    return f"{m // 60} sa {m % 60} dk" if m >= 60 else f"{m} dk"


def on_game_change(old, old_since, new):
    try:
        if old != "0":
            g = find_game(old)
            name = g["name"] if g else old
            mins = max(0, round((time.time() - old_since) / 60)) if old_since else 0
            by_app = time.time() - _state.get("stop_req", 0) < 90
            if g and not by_app:
                time.sleep(4)
            if g and not by_app and crashed(g):
                log(f"{name} çöktü")
                notify("crash", "Oyun çöktü", f"{name} beklenmedik şekilde kapandı ({fmt_min(mins)} oynandı).", old)
            else:
                notify("game", "Oyun kapandı", f"{name} · {fmt_min(mins)} oynandı", old)
            threading.Thread(target=lambda: (check_badges(), chat_register()), daemon=True).start()
            if mins >= 1:
                discord_auto("dc_end", f"🏁 **{persona()}**, **{name}** oturumunu bitirdi ({fmt_min(mins)}).")
        try:
            chat_presence()
        except Exception as e:
            log(f"sohbet durumu: {e}")
        if old == "0" and new != "0":
            game_mode(True)
        elif new == "0":
            game_mode(False)
        if new != "0":
            g = find_game(new)
            name = g["name"] if g else new
            notify("game", "Oyun açıldı", f"{name} başladı", new)
            discord_auto("dc_start", f"🎮 **{persona()}** şimdi **{name}** oynuyor!", new, name)
    except Exception as e:
        log(f"oyun değişimi: {e}")


def check_downloads():
    cur = {g["appid"]: g["name"] for g in read_games(force=True) if g.get("dl")}
    prev = _state.get("dls")
    if prev is not None:
        for appid, name in prev.items():
            g = find_game(appid)
            if appid not in cur and g and not g.get("dl"):
                notify("dl", "İndirme bitti", f"{name} indirildi, oynamaya hazır.", appid)
    _state["dls"] = cur


# ---------- Canlı performans ----------
_perf = {"hist": [], "req": 0}
_ct = {"t": 0, "v": None, "fail": 0}


def gpu_stats():
    try:
        o = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW).stdout.splitlines()[0].split(",")
    except Exception:
        return None

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    u, t, mu, mt, pw = (num(x.strip()) for x in (o + ["", "", "", "", ""])[:5])
    if u is None:
        return None
    return {"gu": round(u), "gt": round(t) if t is not None else None,
            "vm": round(100 * mu / mt) if mu is not None and mt else None,
            "gw": round(pw) if pw is not None else None}


def cpu_temp():
    """LibreHardwareMonitor / OpenHardwareMonitor açıksa işlemci sıcaklığı."""
    now = time.time()
    if now - _ct["t"] < (6 if _ct["fail"] < 3 else 60):
        return _ct["v"]
    _ct["t"] = now
    for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
        ps = (f"Get-CimInstance -Namespace {ns} -ClassName Sensor -ErrorAction Stop | "
              "Where-Object { $_.SensorType -eq 'Temperature' -and $_.Identifier -match 'cpu' } | "
              "Select-Object Name,Value | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=8,
                                 creationflags=NO_WINDOW).stdout.decode("utf-8", "ignore").strip()
            if not out:
                continue
            data = json.loads(out)
            data = [data] if isinstance(data, dict) else data
            pick = next((x for x in data if re.search(r"package|tctl|tdie", x.get("Name") or "", re.I)), None) \
                or max(data, key=lambda x: x.get("Value") or 0)
            _ct.update(v=round(float(pick["Value"])), fail=0)
            return _ct["v"]
        except Exception:
            continue
    _ct["fail"] += 1
    _ct["v"] = None
    return None


def rtss_fps(game_dir=""):
    """RivaTuner Statistics Server (MSI Afterburner ile gelir) açıksa oyunun FPS'i."""
    k = ctypes.windll.kernel32
    k.OpenFileMappingW.restype = ctypes.c_void_p
    k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.MapViewOfFile.restype = ctypes.c_void_p
    k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
    k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenFileMappingW(4, 0, "RTSSSharedMemoryV2")
    if not h:
        return None
    try:
        v = k.MapViewOfFile(h, 4, 0, 0, 0)
        if not v:
            return None
        try:
            sig, ver, esz, aoff, asz = struct.unpack_from("<5I", ctypes.string_at(v, 20))
            if sig != 0x52545353 or ver < 0x00020000 or esz < 284 or asz > 512:
                return None
            tick = k.GetTickCount() & 0xFFFFFFFF
            root, best = game_dir.lower().rstrip("\\/"), None
            for i in range(asz):
                e = ctypes.string_at(v + aoff + i * esz, 284)
                pid = struct.unpack_from("<I", e, 0)[0]
                if not pid:
                    continue
                t0, t1, frames = struct.unpack_from("<III", e, 268)
                if t1 <= t0 or not frames or (tick - t1) & 0xFFFFFFFF > 3000:
                    continue
                name = e[4:264].split(b"\0")[0].decode("mbcs", "ignore").lower()
                score = (bool(root) and name.startswith(root), t1)
                if best is None or score > best[0]:
                    best = (score, 1000.0 * frames / (t1 - t0))
            return round(best[1]) if best else None
        finally:
            k.UnmapViewOfFile(v)
    finally:
        k.CloseHandle(h)


def perf_loop():
    """Bilgisayar sekmesi açıkken 2 saniyede bir ölçüm alır (kapalıyken boşta bekler)."""
    prev = _cpu_times()
    while True:
        time.sleep(2)
        cur = _cpu_times()
        di, dt = cur[0] - prev[0], cur[1] - prev[1]
        prev = cur
        idle = time.time() - _perf["req"]
        if idle > 20:
            if idle > 120:
                _perf["hist"] = []
            continue
        try:
            m = _MEM()
            m.len = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            app = running_appid()
            game = find_game(app) if app != "0" else None
            try:
                fps = rtss_fps(game["dir"] if game else "")
            except Exception:
                fps = None
            smp = {"t": int(time.time()), "cpu": round(100 * (dt - di) / dt) if dt else 0, "ram": m.load,
                   "fps": fps, "ct": cpu_temp(), "gu": None, "gt": None, "vm": None, "gw": None}
            smp.update(gpu_stats() or {})
            _perf["hist"] = (_perf["hist"] + [smp])[-60:]
        except Exception as e:
            log(f"performans: {e}")


def manifest():
    return json.dumps({"name": "Oyunlarım", "short_name": "Oyunlarım", "start_url": "/", "display": "standalone",
                       "background_color": "#1b2838", "theme_color": "#1b2838", "lang": "tr",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]})



# ---------- Dosya aktarımı ----------
SHARE = os.path.join(_BASE, "Telefon Aktarım")
MAX_UP = 1024 ** 3  # dosya başına 1 GB
_dl_tokens = {}


def share_dir():
    os.makedirs(SHARE, exist_ok=True)
    return SHARE


def safe_name(n):
    n = os.path.basename(str(n).replace("\\", "/")).strip().strip(".")
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n)[:150].strip()
    return n or "dosya"


def unique_path(name):
    base, ext = os.path.splitext(name)
    path, i = os.path.join(share_dir(), name), 1
    while os.path.exists(path):
        path = os.path.join(SHARE, f"{base} ({i}){ext}")
        i += 1
    return path


def list_share():
    out = []
    for e in os.scandir(share_dir()):
        if e.is_file() and not e.name.endswith(".part") and not e.name.startswith(".") and e.name != "desktop.ini":
            st = e.stat()
            out.append({"name": e.name, "size": st.st_size, "t": int(st.st_mtime)})
    return sorted(out, key=lambda x: -x["t"])


def share_file(name):
    path = os.path.join(share_dir(), safe_name(name))
    return path if os.path.isfile(path) else None



# ---------- Başarımlar ----------
_ach = {}


def achievements(appid):
    sid = login_user()[0]
    if not api_key() or not sid:
        return {"error": "key"}
    c = _ach.get(appid)
    if c and time.time() - c[0] < 300:
        return c[1]
    try:
        pa = steam_api("ISteamUserStats/GetPlayerAchievements/v1/", appid=appid, steamid=sid, l="turkish")["playerstats"]
    except urllib.error.HTTPError as e:
        return {"error": "none" if e.code in (400, 403) else "net"}
    except Exception as e:
        log(f"başarımlar: {e}")
        return {"error": "net"}
    if not pa.get("success") or not pa.get("achievements"):
        return {"error": "none"}
    schema, glob = {}, {}
    try:
        for a in steam_api("ISteamUserStats/GetSchemaForGame/v2/", appid=appid, l="turkish")["game"]["availableGameStats"]["achievements"]:
            schema[a["name"]] = a
    except Exception as e:
        log(f"başarım şeması: {e}")
    try:
        for a in steam_api("ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/", gameid=appid)["achievementpercentages"]["achievements"]:
            glob[a["name"]] = round(float(a.get("percent", 0)), 1)
    except Exception:
        pass
    out = []
    for a in pa["achievements"]:
        sc = schema.get(a["apiname"], {})
        done = bool(a.get("achieved"))
        hidden = bool(sc.get("hidden")) and not done
        out.append({"name": a.get("name") or sc.get("displayName") or a["apiname"],
                    "desc": "Gizli başarım" if hidden else (a.get("description") or sc.get("description") or ""),
                    "icon": sc.get("icon" if done else "icongray", ""), "done": done,
                    "t": a.get("unlocktime", 0), "pct": glob.get(a["apiname"])})
    res = {"total": len(out), "done": sum(x["done"] for x in out), "list": out}
    _ach[appid] = (time.time(), res)
    return res


# ---------- Kütüphane (kur / güncelle / kaldır) ----------
_owned = {"t": 0, "sid": "", "data": None}


def owned_games():
    sid = login_user()[0]
    if not api_key() or not sid:
        return None
    if _owned["data"] is not None and _owned["sid"] == sid and time.time() - _owned["t"] < 600:
        return _owned["data"]
    o = steam_api("IPlayerService/GetOwnedGames/v1/", steamid=sid, include_appinfo=1,
                  include_played_free_games=1)["response"]
    data = [{"appid": str(g["appid"]), "name": g.get("name", ""), "hours": round(g.get("playtime_forever", 0) / 60)}
            for g in o.get("games", [])]
    _owned.update(t=time.time(), sid=sid, data=data)
    return data


def disks():
    seen, out = set(), []
    steam = find_steam()
    for lib in (library_dirs(steam) if steam else []):
        drive = os.path.splitdrive(lib)[0] or lib
        if drive.upper() in seen:
            continue
        seen.add(drive.upper())
        try:
            u = shutil.disk_usage(lib)
            out.append({"drive": drive, "free": u.free, "total": u.total})
        except OSError:
            pass
    return out


def library_info():
    gs = read_games(force=True)
    inst = {g["appid"] for g in gs}
    upd = [{"appid": g["appid"], "name": g["name"]} for g in gs if not g.get("dl") and g.get("flags", 0) & (2 | 512)]
    res = {"disks": disks(), "updates": upd, "has_key": bool(api_key()), "notinst": None}
    try:
        og = owned_games()
        if og is not None:
            res["notinst"] = sorted([g for g in og if g["appid"] not in inst and g["name"]],
                                    key=lambda g: (-g["hours"], g["name"].lower()))
    except Exception as e:
        log(f"sahip olunan oyunlar: {e}")
    return res


# ---------- Uzaktan fare ----------
MOUSE_BTN = {"left": (2, 4), "right": (8, 16), "middle": (0x20, 0x40)}


def mouse(body):
    u = ctypes.windll.user32
    clamp = lambda v, m: max(-m, min(m, int(v or 0)))
    dx, dy, sc = clamp(body.get("dx"), 3000), clamp(body.get("dy"), 3000), clamp(body.get("scroll"), 2400)
    if dx or dy:
        u.mouse_event(0x0001, dx, dy, 0, 0)
    if sc:
        u.mouse_event(0x0800, 0, 0, sc & 0xFFFFFFFF, 0)
    b = MOUSE_BTN.get(body.get("btn"))
    act = body.get("act", "click")
    if b:
        if act in ("click", "down"):
            u.mouse_event(b[0], 0, 0, 0, 0)
        if act in ("click", "up"):
            u.mouse_event(b[1], 0, 0, 0, 0)


# ---------- PC sesini telefona aktarma (WASAPI loopback) ----------
_audio_inst = {"busy": False, "msg": "", "gen": 0}


def audio_lib():
    try:
        import pyaudiowpatch  # noqa: F401
        return True
    except ImportError:
        return False


def install_audio_lib():
    if getattr(sys, "frozen", False):
        _audio_inst["msg"] = "Ses kütüphanesi bu sürümde gömülü."
        return
    if _audio_inst["busy"]:
        return
    _audio_inst.update(busy=True, msg="Kuruluyor…")

    def run():
        exe = sys.executable
        if os.path.basename(exe).lower().startswith("pythonw"):
            alt = os.path.join(os.path.dirname(exe), "python.exe")
            exe = alt if os.path.exists(alt) else exe
        try:
            r = subprocess.run([exe, "-m", "pip", "install", "--user", "PyAudioWPatch"], capture_output=True, text=True,
                               timeout=600, creationflags=NO_WINDOW)
            ok = r.returncode == 0
            _audio_inst["msg"] = "Kuruldu." if ok else "Kurulamadı: " + (r.stderr or r.stdout).strip()[-160:]
            log("PyAudioWPatch kurulumu: " + ("tamam" if ok else (r.stderr or r.stdout)[-300:]))
            if ok:
                import importlib, site
                importlib.invalidate_caches()
                try:
                    usp = site.getusersitepackages()
                    if usp not in sys.path:
                        sys.path.append(usp)
                except Exception:
                    pass
        except Exception as e:
            _audio_inst["msg"] = f"Kurulamadı: {e}"
        _audio_inst["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def audio_stream(handler, mic=False):
    """Varsayılan hoparlörün (ya da mikrofonun) sesini 16 bit mono PCM olarak akıtır."""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    stream = None
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        spk = p.get_device_info_by_index(wasapi["defaultInputDevice" if mic else "defaultOutputDevice"])
        if not mic and not spk.get("isLoopbackDevice"):
            spk = next((d for d in p.get_loopback_device_info_generator() if spk["name"] in d["name"]), None)
        if not spk:
            return handler._send(500, '{"error":"device"}')
        ch, rate = min(2, int(spk["maxInputChannels"])), int(spk["defaultSampleRate"])
        step = 2 if rate >= 44100 else 1
        frames = rate // 20  # 50 ms
        stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True, frames_per_buffer=frames,
                        input_device_index=spk["index"])
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("X-Rate", str(rate // step))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.close_connection = True
        log(f"ses aktarımı başladı ({spk['name']}, {rate} Hz)")
        _audio_inst["gen"] += 1
        gen = _audio_inst["gen"]
        while gen == _audio_inst["gen"]:
            data = array.array("h", stream.read(frames, exception_on_overflow=False))
            out = array.array("h", [0]) * (len(data) // (ch * step))
            j = 0
            for i in range(0, len(data) - ch + 1, ch * step):
                out[j] = (data[i] + data[i + 1]) // 2 if ch > 1 else data[i]
                j += 1
            handler.wfile.write(out[:j].tobytes())
            handler.wfile.flush()
    except (ConnectionError, OSError):
        pass
    except Exception as e:
        log(f"ses aktarımı: {e!r}")
    finally:
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        p.terminate()
        log("ses aktarımı bitti")


# ---------- Aylık rapor ----------
def month_report(y, m):
    start = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = time.mktime((ny, nm, 1, 0, 0, 0, 0, 0, -1))
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    pstart = time.mktime((py, pm, 1, 0, 0, 0, 0, 0, -1))
    sess = [tuple(x) for x in _data().get("sessions", [])]
    if _state["app"] != "0" and _state["since"]:
        sess.append((_state["app"], _state["since"], time.time()))
    per, days, hours, wd = {}, {}, [0] * 24, [0] * 7
    total = prev = count = 0
    longest = None
    for appid, st, en in sess:
        prev += max(0, min(en, start) - max(st, pstart))
        a, b = max(st, start), min(en, end)
        if b <= a:
            continue
        count += 1
        dur = b - a
        total += dur
        per[appid] = per.get(appid, 0) + dur
        if not longest or dur > longest[0]:
            longest = (dur, appid, a)
        t = a
        while t < b:
            lt = time.localtime(t)
            nxt = min(b, t - lt.tm_min * 60 - lt.tm_sec + 3600)
            seg = nxt - t
            hours[lt.tm_hour] += seg
            wd[lt.tm_wday] += seg
            key = lt.tm_mday
            days[key] = days.get(key, 0) + seg
            t = nxt
    name = lambda a: (find_game(a) or {}).get("name", a)
    best_day = max(days.items(), key=lambda x: x[1]) if days else None
    streak = cur = 0
    for d in range(1, 32):
        cur = cur + 1 if days.get(d) else 0
        streak = max(streak, cur)
    mins = lambda x: round(x / 60)
    return {"y": y, "m": m, "total": mins(total), "prev": mins(prev), "sessions": count, "days": len(days),
            "streak": streak, "avg": mins(total / count) if count else 0,
            "top": [{"appid": a, "name": name(a), "min": mins(v)} for a, v in sorted(per.items(), key=lambda x: -x[1])[:8]],
            "longest": {"appid": longest[1], "name": name(longest[1]), "min": mins(longest[0]), "t": int(longest[2])} if longest else None,
            "best_day": {"d": best_day[0], "min": mins(best_day[1])} if best_day else None,
            "hours": [mins(h) for h in hours], "wd": [mins(w) for w in wd],
            "first": int(min((x[1] for x in sess), default=time.time()))}


def home_info():
    cur = running_appid()
    g = find_game(cur) if cur != "0" else None
    gs = read_games()
    act = activity()
    return {"name": persona(), "appid": cur, "game": g["name"] if g else "",
            "elapsed": int(time.time() - _state["since"]) if cur != "0" and _state["since"] and _state["app"] == cur else 0,
            "today": act["days"][-1]["min"], "streak": act["streak"], "week": act["total"],
            "goal": _data().get("goal", 0), "sys": system_info(),
            "updates": [{"appid": x["appid"], "name": x["name"]} for x in gs if not x.get("dl") and x.get("flags", 0) & (2 | 512)],
            "files": len(list_share()),
            "sales": [g for g in (_wl["data"] or []) if g["pct"]][:6],
            "events": upcoming_events(), "fnow": friends_now(), "anns": _data().get("anns", [])[-3:]}



# ---------- Görev yöneticisi ----------
PROTECTED = {"system", "idle", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
             "dwm", "explorer", "fontdrvhost", "memory compression", "sihost", "ctfmon", "runtimebroker", "taskhostw",
             "audiodg", "conhost", "msmpeng", "securityhealthservice", "spoolsv", "searchindexer", "python", "pythonw",
             "py", "pyw", "tailscaled", "tailscale-ipn", "claude", "startmenuexperiencehost", "shellexperiencehost",
             "textinputhost", "searchhost", "lockapp", "wudfhost", "dllhost", "nvcontainer", "nvdisplay.container"}
_procs = {}


def proc_list():
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $a=@{}; Get-Process | ForEach-Object { $a[$_.Id]=$_.CPU }; "
          "Start-Sleep -Milliseconds 1000; Get-Process | ForEach-Object { [pscustomobject]@{i=$_.Id;n=$_.ProcessName;"
          "c=$_.CPU;p=$a[$_.Id];m=$_.WorkingSet64;t=$_.MainWindowTitle;d=$_.Description} } | ConvertTo-Json -Compress")
    raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                         creationflags=NO_WINDOW).stdout.decode("utf-8", "ignore").lstrip("﻿").strip()
    data = json.loads(raw) if raw else []
    data = [data] if isinstance(data, dict) else data
    ncpu = os.cpu_count() or 1
    groups = {}
    for x in data:
        n = (x.get("n") or "").strip()
        if not n or x.get("i") in (0, 4, os.getpid()):
            continue
        g = groups.setdefault(n.lower(), {"name": n, "desc": "", "title": "", "cpu": 0.0, "mem": 0, "pids": []})
        if x.get("c") is not None and x.get("p") is not None:
            g["cpu"] += max(0.0, (x["c"] - x["p"]) * 100 / ncpu)
        g["mem"] += x.get("m") or 0
        g["pids"].append(x["i"])
        g["desc"] = g["desc"] or (x.get("d") or "")
        g["title"] = g["title"] or (x.get("t") or "")
    _procs.clear()
    _procs.update({k: v["pids"] for k, v in groups.items()})
    out = [{"name": g["name"], "desc": g["desc"], "title": g["title"][:80], "cpu": round(g["cpu"], 1),
            "mem": round(g["mem"] / 2**20), "n": len(g["pids"]), "sys": k in PROTECTED}
           for k, g in groups.items()]
    out.sort(key=lambda g: (-g["cpu"], -g["mem"]))
    return out[:60]


def kill_proc(name):
    k = str(name).lower().strip()
    if not k or k in PROTECTED or k not in _procs:
        return False
    for pid in _procs[k]:
        if pid != os.getpid():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    log(f"görev yöneticisinden kapatıldı: {name}")
    return True


# ---------- Monitör: parlaklık, gece ışığı, ekranı kapat ----------
class _PM(ctypes.Structure):
    _fields_ = [("h", ctypes.c_void_p), ("d", ctypes.c_wchar * 128)]


def _phys_monitors():
    u, dx = ctypes.windll.user32, ctypes.windll.dxva2
    mons = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t)

    def cb(hm, hdc, rc, lp):
        mons.append(hm)
        return 1
    u.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, CB, ctypes.c_ssize_t]
    u.EnumDisplayMonitors(None, None, CB(cb), 0)
    dx.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    dx.GetPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    out = []
    for hm in mons:
        n = ctypes.c_uint32()
        if not dx.GetNumberOfPhysicalMonitorsFromHMONITOR(hm, ctypes.byref(n)) or not n.value:
            continue
        arr = (_PM * n.value)()
        if dx.GetPhysicalMonitorsFromHMONITOR(hm, n.value, arr):
            out += list(arr)
    return out


def _ddc(fn):
    dx = ctypes.windll.dxva2
    dx.GetMonitorBrightness.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint32)] * 3
    dx.SetMonitorBrightness.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dx.DestroyPhysicalMonitor.argtypes = [ctypes.c_void_p]
    pms = _phys_monitors()
    try:
        return fn(dx, pms)
    finally:
        for pm in pms:
            dx.DestroyPhysicalMonitor(pm.h)


def _wmi_brightness(value=None):
    if value is None:
        ps = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | Select-Object -First 1).CurrentBrightness"
    else:
        ps = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | "
              f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=0;Brightness={int(value)}}}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.strip()
        return int(out.splitlines()[0]) if value is None and out else (True if value is not None else None)
    except Exception:
        return None


def monitors():
    def run(dx, pms):
        res = []
        for i, pm in enumerate(pms):
            mn, cur, mx = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
            ok = dx.GetMonitorBrightness(pm.h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx))
            res.append({"id": f"d{i}", "name": pm.d.strip() or f"Monitör {i + 1}",
                        "v": round(100 * (cur.value - mn.value) / max(1, mx.value - mn.value)) if ok else None})
        return res
    try:
        res = _ddc(run)
    except Exception as e:
        log(f"monitör listesi: {e}")
        res = []
    if not any(m["v"] is not None for m in res):
        w = _wmi_brightness()
        if w is not None:
            res = [{"id": "w0", "name": "Dahili ekran", "v": w}] + [m for m in res if m["v"] is not None]
    return {"list": res, "warm": _state.get("warm", 0)}


def set_brightness(mid, v):
    v = max(0, min(100, int(v)))
    if mid == "w0":
        return bool(_wmi_brightness(v))

    def run(dx, pms):
        i = int(mid[1:])
        if not 0 <= i < len(pms):
            return False
        mn, cur, mx = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
        if not dx.GetMonitorBrightness(pms[i].h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)):
            return False
        return bool(dx.SetMonitorBrightness(pms[i].h, mn.value + round((mx.value - mn.value) * v / 100)))
    try:
        return _ddc(run)
    except Exception as e:
        log(f"parlaklık: {e}")
        return False


class _DD(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("name", ctypes.c_wchar * 32), ("str", ctypes.c_wchar * 128),
                ("flags", ctypes.c_uint32), ("id", ctypes.c_wchar * 128), ("key", ctypes.c_wchar * 128)]


WARM = [(1, 1, 1), (1, .93, .84), (1, .86, .70), (1, .79, .58), (1, .72, .48)]


def set_warmth(level):
    """Gece ışığı: ekran kartının renk eğrisini sıcak tona çeker (0 = kapalı)."""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    g.CreateDCW.restype = ctypes.c_void_p
    g.CreateDCW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p]
    g.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    level = max(0, min(len(WARM) - 1, int(level)))
    applied = 0
    for lv in range(level, -1, -1):
        r, gg, b = WARM[lv]
        ramp = (ctypes.c_ushort * 768)()
        for i in range(256):
            v = i * 257
            ramp[i], ramp[256 + i], ramp[512 + i] = int(v * r), int(v * gg), int(v * b)
        ok, i = False, 0
        while True:
            dd = _DD()
            dd.cb = ctypes.sizeof(dd)
            if not u.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            i += 1
            if dd.flags & 1:  # masaüstüne bağlı
                hdc = g.CreateDCW(None, dd.name, None, None)
                if hdc:
                    ok = bool(g.SetDeviceGammaRamp(hdc, ramp)) or ok
                    g.DeleteDC(hdc)
        if ok:
            applied = lv
            break
    _state["warm"] = applied
    return applied


def screen_off():
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)


# ---------- İstek listesi ----------
_wl = {"t": 0, "data": None, "err": ""}


def store_items(ids):
    out = {}
    for i in range(0, len(ids), 50):
        part = ids[i:i + 50]
        req = {"ids": [{"appid": int(a)} for a in part], "context": {"language": "turkish", "country_code": "TR"},
               "data_request": {"include_basic_info": False}}
        try:
            r = steam_api("IStoreBrowseService/GetItems/v1/", input_json=json.dumps(req))
            for it in r.get("response", {}).get("store_items", []):
                bp = it.get("best_purchase_option") or {}
                out[str(it.get("appid"))] = {"name": it.get("name", ""), "price": bp.get("formatted_final_price", ""),
                                             "orig": bp.get("formatted_original_price", ""),
                                             "pct": int(bp.get("discount_pct") or 0), "free": bool(it.get("is_free"))}
        except Exception as e:
            log(f"mağaza bilgisi: {e}")
    missing = [a for a in ids if a not in out or not out[a]["price"]]
    for i in range(0, len(missing), 40):
        part = missing[i:i + 40]
        try:
            url = ("https://store.steampowered.com/api/appdetails?filters=price_overview&cc=tr&appids=" + ",".join(part))
            with urllib.request.urlopen(url, timeout=10) as r:
                js = json.loads(r.read().decode("utf-8"))
            for a in part:
                po = ((js.get(a) or {}).get("data") or {}) if isinstance((js.get(a) or {}).get("data"), dict) else {}
                po = po.get("price_overview") or {}
                if po:
                    o = out.setdefault(a, {"name": "", "free": False})
                    o.update(price=po.get("final_formatted", ""), pct=int(po.get("discount_percent") or 0),
                             orig=po.get("initial_formatted", "") if po.get("discount_percent") else "")
        except Exception as e:
            log(f"fiyat bilgisi: {e}")
    return out


def wishlist(force=False):
    sid = login_user()[0]
    if not api_key() or not sid:
        return {"error": "key"}
    if not force and _wl["data"] is not None and time.time() - _wl["t"] < 1800:
        return {"items": _wl["data"], "t": int(_wl["t"])}
    try:
        items = steam_api("IWishlistService/GetWishlist/v1/", steamid=sid).get("response", {}).get("items", [])
    except Exception as e:
        log(f"istek listesi: {e}")
        return {"error": "net"}
    ids = [str(x["appid"]) for x in items]
    info = store_items(ids)
    out = []
    for x in items:
        a = str(x["appid"])
        i = info.get(a, {})
        out.append({"appid": a, "name": i.get("name") or f"Oyun {a}", "price": i.get("price", ""),
                    "orig": i.get("orig", ""), "pct": i.get("pct", 0), "free": i.get("free", False),
                    "prio": x.get("priority", 0), "added": x.get("date_added", 0)})
    out.sort(key=lambda g: (-g["pct"], g["prio"] or 9999))
    _wl.update(t=time.time(), data=out)
    with _lock:
        d = _data()
        first = "wl_seen" not in d
        seen = set(d.get("wl_seen", []))
        new = [g for g in out if g["pct"] and f"{g['appid']}:{g['pct']}" not in seen]
        d["wl_seen"] = sorted(seen | {f"{g['appid']}:{g['pct']}" for g in out if g["pct"]})[-400:]
        _save(d)
    if not first:
        for g in new[:5]:
            notify("sale", f"İndirim: {g['name']}", f"%{g['pct']} indirim, şimdi {g['price']}" + (f" (önce {g['orig']})" if g["orig"] else ""), g["appid"])
    return {"items": out, "t": int(_wl["t"])}


# ---------- Oyun modu ----------
GM_DEFAULT = {"on": False, "power": True, "open": [], "close": ""}
HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"


def gm_settings(d=None):
    return {**GM_DEFAULT, **(d if d is not None else _data()).get("gm", {})}


def _active_scheme():
    try:
        out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, errors="ignore",
                             timeout=10, creationflags=NO_WINDOW).stdout
        m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", out)
        return m.group(0) if m else ""
    except Exception:
        return ""


def game_mode(start):
    gm = gm_settings()
    if not gm["on"]:
        return
    if start:
        if gm["power"]:
            cur = _active_scheme()
            if cur and cur.lower() != HIGH_PERF:
                _state["prev_scheme"] = cur
                r = subprocess.run(["powercfg", "/setactive", HIGH_PERF], capture_output=True, creationflags=NO_WINDOW)
                if r.returncode:
                    subprocess.run(["powercfg", "/setactive", "SCHEME_MIN"], capture_output=True, creationflags=NO_WINDOW)
        apps = {a["name"]: a["path"] for a in _data().get("apps", [])}
        for n in gm["open"]:
            if n in apps:
                try:
                    os.startfile(apps[n])
                except OSError as e:
                    log(f"oyun modu açamadı {n}: {e}")
        for exe in [x.strip() for x in gm["close"].split(",") if x.strip()]:
            name = exe if exe.lower().endswith(".exe") else exe + ".exe"
            if name[:-4].lower() not in PROTECTED:
                subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, creationflags=NO_WINDOW)
        log("oyun modu açıldı")
    else:
        prev = _state.pop("prev_scheme", "")
        if prev:
            subprocess.run(["powercfg", "/setactive", prev], capture_output=True, creationflags=NO_WINDOW)
            log("oyun modu kapandı, güç planı geri alındı")


# ---------- Ekran görüntüleri ----------
_shots = {"t": 0, "list": [], "tok": secrets.token_urlsafe(16)}
_thumbs = {}


def known_folder(guid):
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    h = guid.replace("-", "")
    gd = GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(h[16:])))
    p = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(gd), 0, None, ctypes.byref(p)) == 0:
            return p.value
    except Exception:
        pass
    return None


def screenshots():
    if time.time() - _shots["t"] < 20:
        return _shots["list"]
    items = []
    steam = find_steam()
    if steam:
        for d in glob.glob(os.path.join(steam, "userdata", "*", "760", "remote", "*", "screenshots")):
            appid = os.path.basename(os.path.dirname(d))
            for f in glob.glob(os.path.join(d, "*.jpg")):
                th = os.path.join(d, "thumbnails", os.path.basename(f))
                items.append((f, appid, "Steam", th if os.path.exists(th) else ""))
    shots = known_folder("b7bede81-df94-4682-a7d8-57a52620b86f") or os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
    vids = known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC") or os.path.join(os.path.expanduser("~"), "Videos")
    for folder, src in ((shots, "Windows"), (os.path.join(vids, "Captures"), "Oyun Çubuğu")):
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            for f in glob.glob(os.path.join(folder, ext)):
                items.append((f, "", src, ""))
    out = []
    for f, appid, src, th in items:
        try:
            out.append({"path": f, "appid": appid, "src": src, "th": th, "t": int(os.path.getmtime(f)),
                        "size": os.path.getsize(f)})
        except OSError:
            pass
    out.sort(key=lambda x: -x["t"])
    _shots.update(t=time.time(), list=out[:400])
    return _shots["list"]


def shot_thumb(it):
    if it["th"]:
        with open(it["th"], "rb") as f:
            return f.read(), "image/jpeg"
    if it["path"] in _thumbs:
        return _thumbs[it["path"]], "image/jpeg"
    try:
        from PIL import Image
        import io
        im = Image.open(it["path"])
        im.thumbnail((480, 270))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
        if len(_thumbs) > 300:
            _thumbs.clear()
        _thumbs[it["path"]] = buf.getvalue()
        return _thumbs[it["path"]], "image/jpeg"
    except Exception:
        with open(it["path"], "rb") as f:
            return f.read(), mimetypes.guess_type(it["path"])[0] or "image/png"


# ---------- Telefondan PC'ye sesli mesaj ----------
def play_wav(data):
    import winsound
    fn = os.path.join(tempfile.gettempdir(), f"steam_remote_ses_{secrets.token_hex(4)}.wav")
    with open(fn, "wb") as f:
        f.write(data)

    def run():
        try:
            winsound.PlaySound(fn, winsound.SND_FILENAME)
        except Exception as e:
            log(f"ses çalınamadı: {e}")
        finally:
            try:
                os.remove(fn)
            except OSError:
                pass
    threading.Thread(target=run, daemon=True).start()


TLS_PEM = """-----BEGIN CERTIFICATE-----
MIIDMTCCAhmgAwIBAgIUIszaxpLHIhS+zyQOz+4X8AyjyTowDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNU3RlYW0gVXpha3RhbjAeFw0yNjEwMjUxNDQ4MjBaFw0z
NjEwMjIxNDQ4MjBaMBgxFjAUBgNVBAMMDVN0ZWFtIFV6YWt0YW4wggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQCMbnZb0N2Hfa53TReZ48WGgMcl1UTbo/xB
Mb7A9KxVaU/hoGPS9aeShfcIRm6FfsezhABiZZw+JxUHcmkENvAmfVCx+ThsYRv3
3xWkERzM8tOU24+bLsk8ZEwm3YsywZyVacB+kd3UsQt7EWbzfs9/W5P6USQc9JFY
CfDNdhQEDhmMyCrDrn34r70wH76C3QYhKaQ6pkcSbP16LhGWHN2yNDXaMwBqQDkC
c5FcJvQuNoMUsyJzSar5Mja9MJiDrGwV9HFr3K9kfgbKXXVBRvodcgblja6jyIQT
oKgmiTZH5u1xLewedJhhuTvWtRYEcjADutJGx6LwVo3KTaREY4upAgMBAAGjczBx
MB0GA1UdDgQWBBTU6b/wOx9+Qu8/twhlacmgBsWSHTAfBgNVHSMEGDAWgBTU6b/w
Ox9+Qu8/twhlacmgBsWSHTAPBgNVHRMBAf8EBTADAQH/MB4GA1UdEQQXMBWCE3N0
ZWFtLXV6YWt0YW4ubG9jYWwwDQYJKoZIhvcNAQELBQADggEBAIgJcBY1NuS2bEMC
BcifLtTCoOaaM2Elw88kPX43pCvfPn6PaBvARdvIAwjIGg/DumqETxJIJsnjUf9A
IXupLVhMLh1bDpTlverMd5f6bI37O0FwB2+F5P7x8djNnhh87pFRbATkNWW/xGuJ
OqPMYrpjvMpc017Hd2cnW08DZj/R7rAUg9rfT8Jy1sbnVd8lDxHu49Im71Vo3nBv
BfKwoqzdBKJPdbokuFobofpnGkGbYvGn7DwIxCgI4C/eKbQ2GMYDkVXd0arICpP5
3FKLqprcgbEzaAfvku+pgXL+v+Xd2e4/mSjQaNWUZB9nnmVcB8hSyWforijyDRix
LwLKTYg=
-----END CERTIFICATE-----
-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCMbnZb0N2Hfa53
TReZ48WGgMcl1UTbo/xBMb7A9KxVaU/hoGPS9aeShfcIRm6FfsezhABiZZw+JxUH
cmkENvAmfVCx+ThsYRv33xWkERzM8tOU24+bLsk8ZEwm3YsywZyVacB+kd3UsQt7
EWbzfs9/W5P6USQc9JFYCfDNdhQEDhmMyCrDrn34r70wH76C3QYhKaQ6pkcSbP16
LhGWHN2yNDXaMwBqQDkCc5FcJvQuNoMUsyJzSar5Mja9MJiDrGwV9HFr3K9kfgbK
XXVBRvodcgblja6jyIQToKgmiTZH5u1xLewedJhhuTvWtRYEcjADutJGx6LwVo3K
TaREY4upAgMBAAECggEABIzApkw/wbxbX5ijEt23EpG0VFuS3Uy9pzHMJxtWRHcn
Vm+YVqdfN7B48jgVGa3IL17dcZvTPdZo0YvXOB4zsZZncqZrMJrgIGu9JG8hEn/R
ISii7du6KQLr4Nd65/RT+bu4AQ05Ayf+AftICMwSGuN6gGsTgzQ4lN2GcREg6D6J
ITyW9mfyQPNqHICxete3Ba8AKfxfZqcHjSGU6/Ff4M9sSFYl1BQndsWUGOkNjXOt
IOtyNg+b8XdpAoPfrbfcgLCRKbgJhpo9f7xNyMd8/OIxf0yQN7NQyequeFwffpif
8maJgJWHili7MVhzSKruX7WiN+swx1BcgoIggQaczQKBgQC/6xesWhvHm43XnpdI
7iRpg5lMMdGqogr6IGF5GkKKX6cUxu/IbUT+L+jxNVkLLIdooda2K+YU3Vlzd+17
lrd9iPJFwylh8qhNxoC0pm0DnHqWOg6ETOsFWJZrqn3KSFh7CVLt2rZYl0JG3KsY
e7Mca6o3jYvVnt4LI1Q7J44ZjQKBgQC7UlkOJQQ+nHyX8DoDBZg528TMvmyPKBCO
D0K0fI7ITPtbtTVGAb7txccnCk1zpwMvGM+Viqo1ENHYlLmKj38H1DuiNI73Kbi9
gB7c9CP/MzFTegb9SOfdcQdxRA+J5eMqP5ADU6HrHe/KDxc09WxpJg0+77+6mqrf
RWg3vESdjQKBgHZN5k7xUPSP2DUVZu6nFj0QyVvlzfhgYWHkuhFb9a603hr5svAb
I4+JWw/tsdEohuQh92he0TsFY9HBGNy5gKza0XXdo1HksokXF1nEyaiTOiIG8dHu
VGX8QOVnuJkMVbaqfbtvuj4MJ+fkuXejMTsQx2HtWEi74OZ+xbqdXIQ1AoGABSUV
3RWXMEEMCwBGF58nty8aEKWKRcRL8BrrMhnZsharMLGhbPLPU6iC98eIqQyFQd3k
la7hTePQX6p2DiAy1Pw3RxU6d/9lW48RSfBKrM+zIx/O4VYHSEW6dpnv81GlE5v7
UJwTbmm8FEil0oaLG7g31Oqw0Wc+GjPF0QISwGECgYAnK+9ewkdm94cbXW5Evu9P
MS8APJnCULqNLfqQMBLssoR7LkmHnJdhoX3IWz5t6Arg7XAloCq5u8qSen2PpVI3
kL5U0nnI5zUssQl1R7GIWkxzP5gDUaUAyDJyrPa+HzW0IVPsO/2CRWZh61M5iE14
08WYUJJfM8dwIxtP9OM8BQ==
-----END PRIVATE KEY-----
"""


class TLSServer(ThreadingHTTPServer):
    """Telefondan mikrofon kullanımı için HTTPS (tarayıcılar mikrofonu sadece güvenli bağlantıda verir)."""
    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        try:
            request.settimeout(15)
            conn = self.ctx.wrap_socket(request, server_side=True)
            conn.settimeout(None)
        except Exception:
            return
        self.RequestHandlerClass(conn, client_address, self)


def start_https():
    try:
        fn = os.path.join(tempfile.gettempdir(), "steam_remote_tls.pem")
        with open(fn, "w", encoding="ascii") as f:
            f.write(TLS_PEM)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(fn)
        srv = TLSServer(("0.0.0.0", PORT + 1), Handler, ctx)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except Exception as e:
        log(f"HTTPS başlatılamadı: {e}")



# ---------- Sohbet (arkadaşlar, odalar) ----------
# Mesajlar ücretsiz ntfy sunucusu üzerinden, uçtan uca şifreli gider; geçmiş bu PC'de saklanır.
NTFY = "https://ntfy.sh"
CHAT = os.path.join(_BASE, "steam_remote_chat.json")
CHAT_MEDIA = os.path.join(_BASE, "steam_remote_chat_media")
_chat_lock = threading.RLock()
_chat_rt = {"resp": None, "dirty": False, "new": False, "ok": False, "err": "", "tok": secrets.token_urlsafe(16),
            "nt": {}}
CODE_RE = re.compile(r"SR-[A-Z2-7]{5}-[A-Z2-7]{5}")
ROOM_RE = re.compile(r"ODA-[A-Z2-7]{4}-[A-Z2-7]{4}")


def _rand_code(prefix, a, b):
    import base64 as b64
    raw = b64.b32encode(secrets.token_bytes(10)).decode()
    return f"{prefix}-{raw[:a]}-{raw[a:a + b]}"


def chat_load():
    try:
        with open(CHAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def chat_save(d):
    tmp = CHAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CHAT)


def _topic(kind, secret):
    return "sr" + hashlib.sha256(f"{kind}|{secret}".encode()).hexdigest()[:30]


def _key(kind, secret):
    return hashlib.sha256(f"key|{kind}|{secret}".encode()).digest()


def _ks(key, nonce, n):
    return b"".join(hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest() for i in range((n + 31) // 32))[:n]


def _xor(data, ks):
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big")


def seal_bytes(key, data):
    nonce = secrets.token_bytes(12)
    ct = _xor(data, _ks(key, nonce, len(data)))
    return nonce + ct + hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]


def open_bytes(key, blob):
    if len(blob) < 28:
        return None
    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
    if not hmac.compare_digest(tag, hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]):
        return None
    return _xor(ct, _ks(key, nonce, len(ct)))


def seal(key, obj):
    return "SR1." + base64.urlsafe_b64encode(seal_bytes(key, json.dumps(obj).encode("utf-8"))).decode()


def unseal(key, text):
    try:
        if not text.startswith("SR1."):
            return None
        raw = open_bytes(key, base64.urlsafe_b64decode(text[4:].encode()))
        return json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return None


def chat_me(d=None):
    d = d if d is not None else chat_load()
    return d.get("me") or {}


def _dm_secret(a, b):
    return "|".join(sorted([a, b]))


def chat_topic_map(d=None):
    """ntfy konu adı -> (tür, sohbet kimliği, anahtar)"""
    d = d if d is not None else chat_load()
    me = chat_me(d).get("code")
    if not me:
        return {}
    tm = {_topic("in", me): ("in", "", _key("in", me))}
    if ADMIN_CODE:
        tm[_topic("cmd", me)] = ("cmd", "", _key("cmd", me))
        tm[_topic("board", ADMIN_CODE)] = ("board", "", _key("board", ADMIN_CODE))
        tm[_topic("ann", ADMIN_CODE)] = ("ann", "", _key("ann", ADMIN_CODE))
        if not is_admin():
            tm[_topic("upd", ADMIN_CODE)] = ("upd", "", _key("upd", ADMIN_CODE))
            tm[_topic("asset", ADMIN_CODE)] = ("asset", "", _key("asset", ADMIN_CODE))
        if is_admin() and me == ADMIN_CODE:
            tm[_topic("adm", ADMIN_CODE)] = ("adm", "", _key("adm", ADMIN_CODE))
    for c, f in d.get("friends", {}).items():
        if f.get("state") == "ok":
            tm[_topic("pr", c)] = ("pr", c, _key("pr", c))
            sec = _dm_secret(me, c)
            tm[_topic("dm", sec)] = ("dm", "d:" + c, _key("dm", sec))
    for r in d.get("rooms", {}):
        tm[_topic("room", r)] = ("room", "r:" + r, _key("room", r))
    return tm


def conv_target(cid, d=None):
    d = d if d is not None else chat_load()
    me = chat_me(d).get("code")
    if cid.startswith("r:") and cid[2:] in d.get("rooms", {}):
        return _topic("room", cid[2:]), _key("room", cid[2:])
    if cid.startswith("d:") and d.get("friends", {}).get(cid[2:], {}).get("state") == "ok":
        sec = _dm_secret(me, cid[2:])
        return _topic("dm", sec), _key("dm", sec)
    return None, None


def chat_refresh_sub(new=True):
    _chat_rt["dirty"] = True
    if new:
        _chat_rt["new"] = True
    r = _chat_rt.get("resp")
    try:
        if r is not None:
            r.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def _cur_game():
    cur = running_appid()
    g = find_game(cur) if cur != "0" else None
    return {"appid": g["appid"], "name": g["name"]} if g else None


def ntfy_publish(topic, text, data=None):
    if STANDALONE:
        return None
    url = f"{NTFY}/{topic}"
    if data is None:
        req = urllib.request.Request(url, data=text.encode("ascii"), method="POST")
    else:
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={"Filename": "sr.bin", "X-Message": text})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return ""
    except urllib.error.HTTPError as e:
        log(f"sohbet gönderim: {e}")
        return "limit" if e.code == 429 else ("big" if e.code == 413 else "net")
    except Exception as e:
        log(f"sohbet gönderim: {e}")
        return "net"


def chat_env(t, **kw):
    me = chat_me()
    return {"v": 1, "mid": secrets.token_hex(8), "t": t, "from": {"c": me.get("code", ""), "n": me.get("name", "")},
            "g": _cur_game(), "ts": int(time.time()), **kw}


UPD_TYPES = ("react", "rsvp", "vote")
MSG_TYPES = ("msg", "img", "invite", "join", "leave", "voice", "event", "poll", "gift", "rps") + UPD_TYPES
MSG_KEYS = ("mid", "t", "from", "text", "g", "ts", "game", "rn", "title", "when", "q", "opts", "dur", "gkind", "gamt", "gitem", "bet", "commit")


def _store_msg(cid, env, img=None, mine=False):
    with _chat_lock:
        d = chat_load()
        lst = d.setdefault("msgs", {}).setdefault(cid, [])
        if any(m.get("mid") == env.get("mid") for m in lst[-300:]) or env.get("mid") in d.get("upd_seen", []):
            return False
        t = env.get("t")
        if t in UPD_TYPES:
            d["upd_seen"] = (d.get("upd_seen", []) + [env.get("mid")])[-500:]
            tgt = next((m for m in reversed(lst) if m.get("mid") == env.get("ref")), None)
            fc, fn = env.get("from", {}).get("c", ""), str(env.get("from", {}).get("n", ""))[:30]
            if tgt is not None and fc:
                if t == "react":
                    re_ = tgt.setdefault("re", {})
                    e = str(env.get("emoji", ""))[:8]
                    if not e or re_.get(fc) == e:
                        re_.pop(fc, None)
                    else:
                        re_[fc] = e
                elif t == "rsvp" and tgt.get("t") == "event" and env.get("ans") in ("yes", "maybe", "no"):
                    tgt.setdefault("rsvp", {})[fc] = {"n": fn, "a": env["ans"]}
                elif t == "vote" and tgt.get("t") == "poll":
                    try:
                        i = int(env.get("i"))
                    except (TypeError, ValueError):
                        i = -1
                    if 0 <= i < len(tgt.get("opts") or []):
                        tgt.setdefault("votes", {})[fc] = i
                tgt["u"] = int(time.time())
            chat_save(d)
            return False
        m = {k: env.get(k) for k in MSG_KEYS if env.get(k) is not None}
        if img:
            m["audio" if t == "voice" else "img"] = img
        if mine:
            d.setdefault("read", {})[cid] = int(time.time())
        lst.append(m)
        lst.sort(key=lambda x: x.get("ts", 0))
        d["msgs"][cid] = lst[-400:]
        if cid.startswith("r:") and env.get("rn") and not mine:
            room = d.get("rooms", {}).get(cid[2:])
            if room is not None and room.get("auto_name"):
                room["name"] = str(env["rn"])[:40]
                room["auto_name"] = False
        if cid.startswith("d:") and env.get("from", {}).get("c") == cid[2:]:
            f = d.get("friends", {}).get(cid[2:])
            if f is not None:
                f.update(game=env.get("g"), seen=int(time.time()), name=str(env["from"].get("n") or f.get("name", ""))[:30])
        chat_save(d)
    return True


def chat_send(cid, t, data=None, **kw):
    topic, key = conv_target(cid)
    if not topic:
        return "conv"
    env = chat_env(t, **kw)
    if cid.startswith("r:"):
        room = chat_load().get("rooms", {}).get(cid[2:], {})
        if not room.get("auto_name"):
            env["rn"] = room.get("name", "")
    img = None
    if data is not None:
        os.makedirs(CHAT_MEDIA, exist_ok=True)
        img = env["mid"] + (".wav" if t == "voice" else ".jpg")
        with open(os.path.join(CHAT_MEDIA, img), "wb") as f:
            f.write(data)
        err = ntfy_publish(topic, seal(key, env), seal_bytes(key, data))
    else:
        err = ntfy_publish(topic, seal(key, env))
    if err:
        return err
    _store_msg(cid, env, img, mine=True)
    return ""


def chat_presence():
    d = chat_load()
    code = chat_me(d).get("code")
    if code and any(f.get("state") == "ok" for f in d.get("friends", {}).values()):
        env = chat_env("status", gs=int(_state["since"]) if running_appid() != "0" and _state["since"] else 0)
        ntfy_publish(_topic("pr", code), seal(_key("pr", code), env))


def chat_notify(cid, title, text):
    last = _chat_rt["nt"].get(cid, 0)
    if time.time() - last > 120:
        _chat_rt["nt"][cid] = time.time()
        notify("chat", title, text)


def chat_handle(ev, tm):
    info = tm.get(ev.get("topic"))
    if not info:
        return
    kind, cid, key = info
    env = unseal(key, ev.get("message", ""))
    if not env or not isinstance(env.get("from"), dict):
        return
    me = chat_me().get("code")
    frm = env["from"]
    if kind == "cmd":
        if env.get("t") == "cmd":
            handle_admin_cmd(env)
        return
    if kind == "asset":
        if env.get("t") == "asset":
            receive_asset(env, ev, key)
        return
    if kind in ("pr", "adm") and env.get("t") == "bgimg":
        receive_bgimg(env, ev, key)
        return
    if kind == "ann":
        if env.get("t") == "ann" and admin_verify(str(env.get("body", "")).encode("utf-8"), str(env.get("sig", ""))):
            try:
                a = json.loads(env["body"])
            except ValueError:
                return
            if store_ann(a) and time.time() - int(a.get("ts", 0)) < 3 * 86400:
                notify("chat", "📢 " + str(a.get("title", "Duyuru"))[:80], str(a.get("text", ""))[:300])
        return
    if kind == "upd":
        att = ev.get("attachment") or {}
        if env.get("t") == "update" and str(env.get("ver", "")) > APP_VER and att.get("url", "").startswith(NTFY + "/") \
                and int(att.get("size") or 0) <= 3 * 2**20:
            try:
                with urllib.request.urlopen(att["url"], timeout=60) as r:
                    raw = open_bytes(key, r.read(3 * 2**20 + 64))
                if raw:
                    threading.Thread(target=apply_update, args=(env, raw), daemon=True).start()
            except Exception as e:
                log(f"güncelleme indirilemedi: {e}")
        return
    if kind == "board":
        att = ev.get("attachment") or {}
        if env.get("t") == "board" and frm.get("c") == ADMIN_CODE and att.get("url", "").startswith(NTFY + "/"):
            try:
                with urllib.request.urlopen(att["url"], timeout=20) as r:
                    raw = open_bytes(key, r.read(512 * 1024))
                b = json.loads(raw.decode("utf-8")) if raw else None
                if isinstance(b, dict) and b.get("t", 0) >= chat_load().get("board", {}).get("t", 0):
                    with _chat_lock:
                        cd = chat_load()
                        cd["board"] = b
                        chat_save(cd)
            except Exception as e:
                log(f"liderlik tablosu alınamadı: {e}")
        return
    if kind == "adm":
        att = ev.get("attachment") or {}
        if env.get("t") == "reg" and att.get("url", "").startswith(NTFY + "/") and int(att.get("size") or 0) <= 128 * 1024:
            try:
                with urllib.request.urlopen(att["url"], timeout=20) as r:
                    raw = open_bytes(key, r.read(128 * 1024 + 64))
                rec = json.loads(raw.decode("utf-8")) if raw else None
                if isinstance(rec, dict) and rec.get("code") == frm.get("c"):
                    db_upsert(rec)
            except Exception as e:
                log(f"kayıt alınamadı: {e}")
        return
    if kind == "in":
        c = str(frm.get("c", ""))
        if not CODE_RE.fullmatch(c) or c == me:
            return
        with _chat_lock:
            d = chat_load()
            fr = d.setdefault("friends", {})
            f = fr.get(c)
            if env.get("t") == "freq" and (not f or f.get("state") != "ok"):
                if f and f.get("state") == "out":
                    f.update(state="ok", name=str(frm.get("n", ""))[:30])
                    chat_save(d)
                    ntfy_publish(_topic("in", c), seal(_key("in", c), chat_env("facc")))
                    chat_refresh_sub()
                    return
                fr[c] = {"name": str(frm.get("n", ""))[:30], "state": "in", "ts": int(time.time())}
                chat_save(d)
                chat_notify("in", "Arkadaşlık isteği", f"{frm.get('n', 'Biri')} seni arkadaş olarak eklemek istiyor.")
            elif env.get("t") == "facc" and f and f.get("state") == "out":
                f.update(state="ok", name=str(frm.get("n", ""))[:30])
                chat_save(d)
                chat_refresh_sub()
                threading.Thread(target=chat_publish_profile, daemon=True).start()
                chat_notify("in", "Arkadaşlık kabul edildi", f"{frm.get('n', 'Arkadaşın')} isteğini kabul etti.")
        return
    if kind == "pr":
        prof = None
        att = ev.get("attachment") or {}
        if env.get("t") == "profile" and att.get("url", "").startswith(NTFY + "/") and int(att.get("size") or 0) <= 64 * 1024:
            try:
                with urllib.request.urlopen(att["url"], timeout=20) as r:
                    raw = open_bytes(key, r.read(64 * 1024 + 64))
                prof = json.loads(raw.decode("utf-8")) if raw else None
            except Exception as e:
                log(f"arkadaş profili alınamadı: {e}")
        with _chat_lock:
            d = chat_load()
            f = d.get("friends", {}).get(cid)
            if f is not None:
                f.update(game=env.get("g"), seen=int(env.get("ts") or time.time()))
                if env.get("t") == "status":
                    f["gs"] = int(env.get("gs") or 0)
                if isinstance(prof, dict):
                    f["profile"] = {k: prof.get(k) for k in ("name", "bio", "av", "frame", "bg", "bgf", "photo", "fav", "level", "hours", "items", "badges", "showcase", "recent")}
                    if prof.get("name"):
                        f["name"] = str(prof["name"])[:30]
                chat_save(d)
        return
    if env.get("t") == "visit":
        if cid.startswith("d:") and frm.get("c") != me:
            with _lock:
                dd = _data()
                vis = dd.setdefault("visitors", {})
                v = vis.get(frm["c"]) or {"n": "", "first": int(time.time()), "count": 0}
                v.update(n=str(frm.get("n", ""))[:30], ts=int(time.time()), count=v.get("count", 0) + 1)
                vis[frm["c"]] = v
                _save(dd)
        return
    if env.get("t") == "rpsmove":
        handle_rps_move(cid, env)
        return
    if env.get("t") == "gift" and frm.get("c") != me and not any(m.get("mid") == env.get("mid") for m in chat_load().get("msgs", {}).get(cid, [])[-300:]):
        apply_gift(env)
    if env.get("t") not in MSG_TYPES:
        return
    img = None
    att = ev.get("attachment") or {}
    if env.get("t") in ("img", "voice"):
        if frm.get("c") == me:
            return  # kendi gönderdiğimiz resim zaten kayıtlı
        try:
            if att.get("url", "").startswith(NTFY + "/") and int(att.get("size") or 0) <= 3 * 2**20:
                with urllib.request.urlopen(att["url"], timeout=30) as r:
                    raw = open_bytes(key, r.read(3 * 2**20 + 64))
                if raw:
                    os.makedirs(CHAT_MEDIA, exist_ok=True)
                    img = re.sub(r"[^a-f0-9]", "", str(env.get("mid", "")))[:16] + (".wav" if env["t"] == "voice" else ".jpg")
                    with open(os.path.join(CHAT_MEDIA, img), "wb") as f:
                        f.write(raw)
        except Exception as e:
            log(f"sohbet dosyası indirilemedi: {e}")
        if not img:
            env["text"] = "(Dosya artık indirilemiyor)"
    if _store_msg(cid, env, img, mine=frm.get("c") == me) and frm.get("c") != me:
        who = str(frm.get("n") or "Arkadaşın")
        body = {"msg": env.get("text", ""), "img": "Resim gönderdi", "invite": f"Seni {(env.get('game') or {}).get('name', 'bir oyun')} oynamaya çağırıyor!",
                "join": "Odaya katıldı", "leave": "Odadan ayrıldı", "voice": "🎤 Sesli mesaj gönderdi",
                "event": f"📅 Oyun gecesi planladı: {env.get('title', '')}", "poll": f"📊 Anket: {env.get('q', '')}",
                "gift": "🎁 Sana bir hediye gönderdi!", "rps": f"✊ Taş-kağıt-makas: {env.get('bet', 0)} jeton bahis"}.get(env["t"], "")
        if env["t"] not in ("join", "leave"):
            room = chat_load().get("rooms", {}).get(cid[2:], {}).get("name") if cid.startswith("r:") else None
            chat_notify(cid, f"{who}" + (f" · {room}" if room else ""), str(body)[:200])


def chat_loop():
    while True:
        tm = chat_topic_map()
        if not tm:
            time.sleep(3)
            continue
        d = chat_load()
        since, now = d.get("since", 0), time.time()
        new = _chat_rt["new"] or not since
        _chat_rt["new"] = False
        _chat_rt["dirty"] = False
        st = "12h" if new else str(int(max(since - 30, now - 43000)))
        resp = None
        try:
            resp = urllib.request.urlopen(f"{NTFY}/{','.join(tm)}/json?since={st}", timeout=120)
            _chat_rt.update(resp=resp, ok=True, err="")
            for line in resp:
                if _chat_rt["dirty"]:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") != "message":
                    continue
                seen = _chat_rt.setdefault("ids", [])
                if ev.get("id") in seen:
                    continue
                seen.append(ev.get("id"))
                del seen[:-600]
                try:
                    chat_handle(ev, tm)
                except Exception as e:
                    log(f"sohbet mesajı işlenemedi: {e!r}")
                with _chat_lock:
                    d = chat_load()
                    d["since"] = max(d.get("since", 0), int(ev.get("time") or 0))
                    chat_save(d)
        except Exception as e:
            if not _chat_rt["dirty"]:
                _chat_rt.update(ok=False, err=str(e)[:100])
                log(f"sohbet bağlantısı: {e}")
                time.sleep(8)
        finally:
            _chat_rt["resp"] = None
            try:
                resp and resp.close()
            except Exception:
                pass


def chat_state():
    d = chat_load()
    me = chat_me(d)
    if not me.get("code"):
        return {"me": None, "suggest": persona()}
    rd = d.get("read", {})
    convs = []
    msgs = d.get("msgs", {})

    def summary(cid, base):
        lst = msgs.get(cid, [])
        last = lst[-1] if lst else None
        base["last"] = {"text": (last.get("text") or {"img": "📷 Resim", "invite": "🎮 Oyun daveti", "voice": "🎤 Sesli mesaj", "event": "📅 Oyun gecesi", "poll": "📊 Anket", "join": "Odaya katıldı",
                                                      "leave": "Odadan ayrıldı"}.get(last.get("t"), "")),
                        "ts": last.get("ts", 0), "n": last.get("from", {}).get("n", ""),
                        "mine": last.get("from", {}).get("c") == me["code"]} if last else None
        base["unread"] = sum(1 for m in lst if m.get("ts", 0) > rd.get(cid, 0) and m.get("from", {}).get("c") != me["code"]
                             and m.get("t") not in ("join", "leave"))
        convs.append(base)
    for c, f in d.get("friends", {}).items():
        if f.get("state") == "ok":
            fresh = f.get("seen", 0) > time.time() - 4 * 3600
            summary("d:" + c, {"id": "d:" + c, "kind": "dm", "name": f.get("name") or c, "code": c,
                               "game": f.get("game") if fresh else None, "prof": f.get("profile")})
    for r, info in d.get("rooms", {}).items():
        mem = {}
        for m in msgs.get("r:" + r, []):
            fc = m.get("from", {}).get("c")
            if fc:
                mem[fc] = {"n": m["from"].get("n", ""), "g": m.get("g"), "left": m.get("t") == "leave"}
        summary("r:" + r, {"id": "r:" + r, "kind": "room", "name": info.get("name") or r, "code": r,
                           "members": [{"c": k, "n": v["n"], "g": v["g"]} for k, v in mem.items() if not v["left"]]})
    convs.sort(key=lambda c: -((c["last"] or {}).get("ts") or 0))
    reqs = [{"code": c, "name": f.get("name", ""), "dir": f["state"]} for c, f in d.get("friends", {}).items()
            if f.get("state") in ("in", "out")]
    return {"me": me, "ok": _chat_rt["ok"], "err": _chat_rt["err"], "convs": convs, "reqs": reqs,
            "unread": sum(c["unread"] for c in convs) + sum(1 for r in reqs if r["dir"] == "in")}


def chat_msgs(cid, after=0):
    d = chat_load()
    me = chat_me(d).get("code")
    out = []
    for m in d.get("msgs", {}).get(cid, []):
        cur = max(m.get("ts", 0), m.get("u", 0))
        if cur <= after:
            continue
        x = dict(m)
        x["_u"] = cur
        x["me"] = m.get("from", {}).get("c") == me
        x["myc"] = me
        for k in ("img", "audio"):
            if m.get(k):
                x[k] = f"/cimg?t={_chat_rt['tok']}&f={m[k]}"
        out.append(x)
    return out[-200:]


def chat_action(b):
    act = b.get("action")
    with _chat_lock:
        d = chat_load()
        me = d.get("me") or {}
        if act == "init":
            name = str(b.get("name", "")).strip()[:30] or prof_get()[0]["name"]
            if not me.get("code"):
                d["me"] = {"code": _rand_code("SR", 5, 5), "name": name}
                d["since"] = int(time.time())
            else:
                d["me"]["name"] = name
            chat_save(d)
            chat_refresh_sub()
            threading.Thread(target=chat_sync, daemon=True).start()
            return {"ok": True}
        if not me.get("code"):
            return {"error": "init"}
        if act == "setname":
            me["name"] = str(b.get("name", "")).strip()[:30] or me["name"]
            chat_save(d)
            return {"ok": True}
        if act == "addfriend":
            c = str(b.get("code", "")).strip().upper()
            if not CODE_RE.fullmatch(c) or c == me["code"]:
                return {"error": "code"}
            f = d.setdefault("friends", {}).get(c)
            if f and f.get("state") == "ok":
                return {"error": "already"}
            if f and f.get("state") == "in":
                b = {"action": "accept", "code": c}
                act = "accept"
            else:
                d["friends"][c] = {"name": "", "state": "out", "ts": int(time.time())}
                chat_save(d)
                err = ntfy_publish(_topic("in", c), seal(_key("in", c), chat_env("freq")))
                return {"error": err} if err else {"ok": True}
        if act in ("accept", "reject", "unfriend"):
            c = str(b.get("code", ""))
            f = d.get("friends", {}).get(c)
            if not f:
                return {"error": "code"}
            if act == "accept":
                f["state"] = "ok"
                chat_save(d)
                err = ntfy_publish(_topic("in", c), seal(_key("in", c), chat_env("facc")))
                chat_refresh_sub()
                threading.Thread(target=chat_publish_profile, daemon=True).start()
                return {"error": err} if err else {"ok": True}
            d["friends"].pop(c, None)
            d.get("msgs", {}).pop("d:" + c, None)
            chat_save(d)
            chat_refresh_sub(False)
            return {"ok": True}
        if act in ("mkroom", "join"):
            code = _rand_code("ODA", 4, 4) if act == "mkroom" else str(b.get("code", "")).strip().upper()
            if not ROOM_RE.fullmatch(code):
                return {"error": "code"}
            name = str(b.get("name", "")).strip()[:40]
            d.setdefault("rooms", {})[code] = {"name": name or ("Oda " + code[4:8]), "auto_name": not name, "ts": int(time.time())}
            chat_save(d)
            chat_refresh_sub()
    if act in ("mkroom", "join"):
        chat_send("r:" + code, "join")
        return {"ok": True, "id": "r:" + code, "code": code}
    if act == "leave":
        cid = str(b.get("id", ""))
        if cid.startswith("r:"):
            chat_send(cid, "leave")
            with _chat_lock:
                d = chat_load()
                d.get("rooms", {}).pop(cid[2:], None)
                d.get("msgs", {}).pop(cid, None)
                chat_save(d)
            chat_refresh_sub(False)
        return {"ok": True}
    if act == "rename":
        cid = str(b.get("id", ""))
        with _chat_lock:
            d = chat_load()
            r = d.get("rooms", {}).get(cid[2:]) if cid.startswith("r:") else None
            if r is not None:
                r["name"] = str(b.get("name", "")).strip()[:40] or r["name"]
                r["auto_name"] = False
                chat_save(d)
        return {"ok": True}
    if act == "read":
        with _chat_lock:
            d = chat_load()
            d.setdefault("read", {})[str(b.get("id", ""))] = int(time.time())
            chat_save(d)
        return {"ok": True}
    if act == "send":
        text = str(b.get("text", "")).strip()[:1000]
        if not text:
            return {"error": "empty"}
        err = chat_send(str(b.get("id", "")), "msg", text=text)
        return {"error": err} if err else {"ok": True}
    if act == "react":
        e = str(b.get("emoji", ""))[:8]
        err = chat_send(str(b.get("id", "")), "react", ref=str(b.get("ref", "")), emoji=e)
        if not err:
            quest_mark("react")
            with _chat_lock:  # kendi tepkimizi hemen uygula
                pass
        return {"error": err} if err else {"ok": True}
    if act in ("rsvp", "vote"):
        kw = {"ref": str(b.get("ref", ""))}
        if act == "rsvp":
            kw["ans"] = str(b.get("ans", ""))
        else:
            kw["i"] = _int(b.get("i"))
        err = chat_send(str(b.get("id", "")), act, **kw)
        return {"error": err} if err else {"ok": True}
    if act == "event":
        title = str(b.get("title", "")).strip()[:80]
        try:
            when = int(b.get("when"))
        except (TypeError, ValueError):
            return {"error": "bad"}
        if not title or when < time.time() - 3600:
            return {"error": "bad"}
        g = find_game(str(b.get("appid", "")))
        err = chat_send(str(b.get("id", "")), "event", title=title, when=when,
                        game={"appid": g["appid"], "name": g["name"]} if g else None)
        if not err:
            with _lock:
                dd = _data()
                dd["q_event"] = dd.get("q_event", 0) + 1
                _save(dd)
        return {"error": err} if err else {"ok": True}
    if act == "poll":
        q = str(b.get("q", "")).strip()[:120]
        opts = [str(o).strip()[:60] for o in (b.get("opts") or []) if str(o).strip()][:6]
        if not q or len(opts) < 2:
            return {"error": "bad"}
        err = chat_send(str(b.get("id", "")), "poll", q=q, opts=opts)
        return {"error": err} if err else {"ok": True}
    if act == "visit":
        c = str(b.get("code", ""))
        if CODE_RE.fullmatch(c):
            threading.Thread(target=send_visit, args=(c,), daemon=True).start()
        return {"ok": True}
    if act == "gift":
        err = gift_send("d:" + str(b.get("code", "")), str(b.get("kind", "")), b.get("amt", 0), str(b.get("item", "")))
        return {"error": err} if err else {"ok": True}
    if act == "rps":
        return rps_start("d:" + str(b.get("code", "")), b.get("bet", 0))
    if act == "rpsmove":
        return rps_accept(str(b.get("id", "")), str(b.get("ref", "")), str(b.get("choice", "")))
    if act == "invite":
        g = find_game(str(b.get("appid", "")))
        if not g:
            return {"error": "game"}
        err = chat_send(str(b.get("id", "")), "invite", text=str(b.get("text", "")).strip()[:200],
                        game={"appid": g["appid"], "name": g["name"]})
        return {"error": err} if err else {"ok": True}
    return {"error": "bad"}



# ---------- Uygulama profili, mağaza, admin ----------
ADMIN_PW = "2112"
START_COINS = 500
CATALOG = [
    # Arka planlar
    {"id": "bg_gece", "k": "bg", "n": "Gece mavisi", "p": 0, "css": "linear-gradient(135deg,#0f2027,#203a43,#2c5364)"},
    {"id": "bg_gunbatimi", "k": "bg", "n": "Gün batımı", "p": 150, "css": "linear-gradient(135deg,#ff512f,#dd2476)"},
    {"id": "bg_okyanus", "k": "bg", "n": "Okyanus", "p": 150, "css": "linear-gradient(160deg,#1a2980,#26d0ce)"},
    {"id": "bg_orman", "k": "bg", "n": "Orman", "p": 150, "css": "linear-gradient(160deg,#134e5e,#71b280)"},
    {"id": "bg_lav", "k": "bg", "n": "Lav", "p": 250, "css": "radial-gradient(circle at 30% 120%,#ff9a00 0,#ff4e00 30%,#3a0a00 75%)"},
    {"id": "bg_yildiz", "k": "bg", "n": "Yıldızlı gece", "p": 300, "css": "radial-gradient(1.5px 1.5px at 20% 30%,#fff 50%,transparent 51%),radial-gradient(1px 1px at 70% 60%,#fff 50%,transparent 51%),radial-gradient(1.5px 1.5px at 85% 20%,#cfe 50%,transparent 51%),radial-gradient(1px 1px at 40% 80%,#fff 50%,transparent 51%),radial-gradient(1px 1px at 55% 15%,#fff 50%,transparent 51%),linear-gradient(180deg,#000428,#004e92)"},
    {"id": "bg_neon", "k": "bg", "n": "Neon ızgara", "p": 350, "css": "linear-gradient(transparent 0 92%,rgba(255,0,200,.55) 92%) 0 0/100% 22px,linear-gradient(90deg,transparent 0 92%,rgba(0,220,255,.45) 92%) 0 0/22px 100%,linear-gradient(180deg,#14002b,#2b0050)"},
    {"id": "bg_aurora", "k": "bg", "n": "Kuzey ışıkları", "p": 400, "css": "linear-gradient(120deg,#00c9ff,#92fe9d,#7f00ff,#00c9ff)", "anim": "flow"},
    {"id": "bg_altin", "k": "bg", "n": "Altın", "p": 500, "css": "linear-gradient(135deg,#bf953f,#fcf6ba,#b38728,#fbf5b7,#aa771c)"},
    {"id": "bg_girdap", "k": "bg", "n": "Galaksi girdabı ✨", "p": 500, "img": "bg_girdap.gif"},
    {"id": "bg_retro", "k": "bg", "n": "Retro gün batımı ✨", "p": 600, "img": "bg_retro.gif", "pix": True},
    {"id": "bg_custom", "k": "bg", "n": "Kendi arka planın", "p": 1000, "custom": True},
    {"id": "bg_matrix", "k": "bg", "n": "Matrix", "p": 450, "css": "repeating-linear-gradient(90deg,rgba(0,255,70,.18) 0 2px,transparent 2px 14px),linear-gradient(180deg,#000,#002b0f)"},
    # Çerçeveler
    {"id": "fr_yok", "k": "frame", "n": "Çerçevesiz", "p": 0, "css": "transparent"},
    {"id": "fr_gumus", "k": "frame", "n": "Gümüş", "p": 100, "css": "linear-gradient(135deg,#e0e0e0,#8e9eab,#eef2f3)"},
    {"id": "fr_altin", "k": "frame", "n": "Altın", "p": 250, "css": "linear-gradient(135deg,#bf953f,#fcf6ba,#b38728,#fbf5b7)", "glow": "0 0 14px rgba(252,246,186,.55)"},
    {"id": "fr_neon", "k": "frame", "n": "Neon mavi", "p": 300, "css": "#00e5ff", "glow": "0 0 16px #00e5ff,0 0 32px rgba(0,229,255,.5)", "anim": "pulse"},
    {"id": "fr_ates", "k": "frame", "n": "Ateş", "p": 400, "css": "conic-gradient(#ff0000,#ff8a00,#ffe600,#ff8a00,#ff0000)", "glow": "0 0 18px rgba(255,120,0,.7)", "anim": "spin"},
    {"id": "fr_buz", "k": "frame", "n": "Buz", "p": 350, "css": "conic-gradient(#e0f7ff,#7fdbff,#ffffff,#7fdbff,#e0f7ff)", "glow": "0 0 14px rgba(127,219,255,.6)", "anim": "spin"},
    {"id": "fr_gokkusagi", "k": "frame", "n": "Gökkuşağı", "p": 600, "css": "conic-gradient(red,orange,yellow,lime,cyan,blue,magenta,red)", "anim": "spin"},
    {"id": "fr_elmas", "k": "frame", "n": "Elmas", "p": 800, "css": "conic-gradient(#b9f2ff,#fff,#a0e9ff,#d6f9ff,#fff,#b9f2ff)", "glow": "0 0 22px rgba(185,242,255,.8)", "anim": "spin"},
    # Avatarlar
    {"id": "av_oyuncu", "k": "av", "n": "Oyuncu", "p": 0, "e": "🎮", "c": "#2f4560"},
    {"id": "av_uzayli", "k": "av", "n": "Uzaylı", "p": 50, "e": "👾", "c": "#5b2a86"},
    {"id": "av_kurt", "k": "av", "n": "Kurt", "p": 80, "e": "🐺", "c": "#37474f"},
    {"id": "av_tilki", "k": "av", "n": "Tilki", "p": 80, "e": "🦊", "c": "#e65100"},
    {"id": "av_robot", "k": "av", "n": "Robot", "p": 100, "e": "🤖", "c": "#00838f"},
    {"id": "av_kafatasi", "k": "av", "n": "Kafatası", "p": 120, "e": "💀", "c": "#212121"},
    {"id": "av_ejderha", "k": "av", "n": "Ejderha", "p": 200, "e": "🐉", "c": "#1b5e20"},
    {"id": "av_aslan", "k": "av", "n": "Aslan", "p": 150, "e": "🦁", "c": "#f9a825"},
    {"id": "av_ninja", "k": "av", "n": "Ninja", "p": 180, "e": "🥷", "c": "#263238"},
    {"id": "av_hayalet", "k": "av", "n": "Hayalet", "p": 120, "e": "👻", "c": "#4a148c"},
    {"id": "av_tac", "k": "av", "n": "Kral", "p": 400, "e": "👑", "c": "#4e342e"},
    {"id": "av_zipla", "k": "av", "n": "Zıpzıp 🐸 (hareketli)", "p": 250, "e": "🐸", "c": "#1b5e20", "anim": "bounce"},
    {"id": "av_don", "k": "av", "n": "Dönen yıldız ⭐ (hareketli)", "p": 300, "e": "⭐", "c": "#4e342e", "anim": "spin"},
    {"id": "av_atan", "k": "av", "n": "Atan kalp ❤️ (hareketli)", "p": 300, "e": "❤️", "c": "#4a0e1a", "anim": "beat"},
    {"id": "av_alev", "k": "av", "n": "Alev 🔥 (hareketli)", "p": 350, "e": "🔥", "c": "#3a0a00", "anim": "flick"},
    {"id": "av_disko", "k": "av", "n": "Disko 🕺 (hareketli)", "p": 450, "e": "🕺", "c": "#2b0050", "anim": "disco"},
    {"id": "av_foto", "k": "av", "n": "Kendi fotoğrafın", "p": 0, "e": "📷", "c": "#2f4560"},
    # Temalar (uygulama renkleri)
    {"id": "th_synthwave", "k": "theme", "n": "Synthwave", "p": 300, "t": ["#1a0b2e", "#2b1055", "#4c1d95", "#fdf4ff", "#c4a1ff", "#ff2ec4"]},
    {"id": "th_kan", "k": "theme", "n": "Kan kırmızısı", "p": 300, "t": ["#120708", "#241012", "#3d1a1d", "#fbeaea", "#c69a9d", "#ff4d5e"]},
    {"id": "th_okyanus", "k": "theme", "n": "Derin okyanus", "p": 300, "t": ["#021a26", "#06303f", "#0b4a5e", "#e3f7ff", "#89b9c9", "#2ee6d6"]},
    {"id": "th_altin", "k": "theme", "n": "Siyah altın", "p": 500, "t": ["#0b0a07", "#17140d", "#2c2616", "#fff8e1", "#bfae7c", "#f5c542"]},
    {"id": "th_matrix", "k": "theme", "n": "Matrix", "p": 400, "t": ["#000a03", "#03170a", "#0a2e16", "#d9ffe4", "#6fbf86", "#00ff66"]},
]
CAT = {c["id"]: c for c in CATALOG}
PROF_DEFAULT = {"name": "", "bio": "", "av": "av_oyuncu", "frame": "fr_yok", "bg": "bg_gece", "fav": "", "photo": "",
                "photo_s": "", "bgf": "", "showcase": []}


def prof_get(d=None):
    d = d if d is not None else _data()
    pr = {**PROF_DEFAULT, **d.get("prof", {})}
    if not pr["name"]:
        pr["name"] = persona()
    owned = set(d.get("owned", [])) | {c["id"] for c in CATALOG if c["p"] == 0}
    return pr, d.get("coins", START_COINS), sorted(owned)


def total_minutes():
    return round(sum(max(0, e - s) for _, s, e in _data().get("sessions", [])) / 60)


def prof_public():
    pr, coins, owned = prof_get()
    g = find_game(pr["fav"]) if pr["fav"] else None
    mins = total_minutes()
    return {"name": pr["name"], "bio": pr["bio"], "av": pr["av"], "frame": pr["frame"], "bg": pr["bg"], "bgf": pr.get("bgf", ""),
            "photo": pr["photo_s"], "fav": {"appid": g["appid"], "name": g["name"]} if g else None,
            "level": 1 + mins // 300, "hours": mins // 60, "items": len(owned), "badges": sorted(_data().get("badges", {})),
            "showcase": [b for b in pr.get("showcase", []) if b in _data().get("badges", {})][:3],
            "recent": [{"appid": t["appid"], "name": t["name"]} for t in activity().get("top", [])[:3]]}


def me_info():
    login = daily_login()
    check_badges()
    pr, coins, owned = prof_get()
    act = activity()
    d = chat_load()
    friends = [c for c, f in d.get("friends", {}).items() if f.get("state") == "ok"]
    mins = total_minutes()
    return {"prof": pr, "coins": coins, "owned": owned, "catalog": CATALOG,
            "stats": {"today": act["days"][-1]["min"], "week": act["total"], "total": mins, "level": 1 + mins // 300,
                      "next": 300 - mins % 300, "friends": len(friends), "streak": act["streak"]},
            "top": act["top"], "note": _data().get("admin_note"), "is_admin": is_admin(), "standalone": STANDALONE, "lang": get_lang(), "login": login,
            "quests": quests(), "wheel": wheel_state(), "ledger": _data().get("ledger", [])[-20:][::-1],
            "badges": [{"id": b, "e": v[0], "n": v[1], "d": v[2], "ts": _data().get("badges", {}).get(b)} for b, v in BADGES.items()],
            "admin_linked": bool(ADMIN_CODE) and not is_admin(),
            "visitors": visitors_list(), "weekly": weekly_summary(), "quiz_done": _data().get("quiz", {}).get("done", False) and _data().get("quiz", {}).get("day") == today_key()}


def prof_action(b):
    act = b.get("action")
    with _lock:
        d = _data()
        pr, coins, owned = prof_get(d)
        if act == "save":
            for k, n in (("name", 30), ("bio", 120)):
                if k in b:
                    pr[k] = str(b[k]).strip()[:n] or (persona() if k == "name" else "")
            for k, kind in (("av", "av"), ("frame", "frame"), ("bg", "bg")):
                v = str(b.get(k, pr[k]))
                if v in CAT and CAT[v]["k"] == kind and v in owned and not (v == "bg_custom" and not pr.get("bgf")):
                    pr[k] = v
            if "fav" in b:
                pr["fav"] = str(b["fav"]) if find_game(str(b["fav"])) else ""
            if isinstance(b.get("showcase"), list):
                have = _data().get("badges", {})
                pr["showcase"] = [str(x) for x in b["showcase"] if str(x) in have][:3]
            if "photo" in b:
                ph, sm = str(b.get("photo", "")), str(b.get("photo_s", ""))
                if ph and not (ph.startswith("data:image/jpeg;base64,") and len(ph) < 120000 and len(sm) < 9000):
                    return {"error": "photo"}
                pr["photo"], pr["photo_s"] = ph, sm
                if ph:
                    pr["av"] = "av_foto"
            d["prof"] = pr
            _save(d)
            res = {"ok": True}
        elif act == "buy":
            it = CAT.get(str(b.get("id", "")))
            if not it:
                return {"error": "item"}
            if it["id"] in owned:
                return {"error": "owned"}
            if coins < it["p"]:
                return {"error": "coins"}
            d["coins"] = coins - it["p"]
            d["owned"] = sorted(set(d.get("owned", [])) | {it["id"]})
            _save(d)
            log(f"mağaza: {it['n']} alındı ({it['p']} jeton)")
            return {"ok": True, "coins": d["coins"]}
        else:
            return {"error": "bad"}
    # İsim değişince sohbetteki ad da güncellensin
    with _chat_lock:
        cd = chat_load()
        if cd.get("me", {}).get("code") and cd["me"].get("name") != pr["name"]:
            cd["me"]["name"] = pr["name"]
            chat_save(cd)
    threading.Thread(target=chat_sync, daemon=True).start()
    return res


def chat_profile_daily():
    time.sleep(20)
    while True:
        try:
            d = chat_load()
            if chat_me(d).get("code") and time.time() - d.get("prof_pub", 0) > 20 * 3600:
                chat_sync()
        except Exception as e:
            log(f"profil paylaşımı: {e}")
        time.sleep(3600)


def chat_publish_profile():
    with _chat_lock:
        d = chat_load()
        d["prof_pub"] = int(time.time())
        chat_save(d)
    code = chat_me(d).get("code")
    if not code:
        return
    key = _key("pr", code)
    env = chat_env("profile")
    data = json.dumps(prof_public()).encode("utf-8")
    ntfy_publish(_topic("pr", code), seal(key, env), seal_bytes(key, data))


def admin_ok(b):
    return hmac.compare_digest(str(b.get("pw", "")).encode(), ADMIN_PW.encode())


def admin_action(b):
    if not is_admin():
        return {"error": "noadmin"}
    if not admin_ok(b):
        time.sleep(1)
        return {"error": "pw"}
    act = b.get("action")
    if act == "check":
        return {"ok": True}
    if act == "coins":
        try:
            v = max(0, min(10**7, int(b.get("v"))))
        except (TypeError, ValueError):
            return {"error": "bad"}
        with _lock:
            d = _data()
            d["coins"] = v
            _save(d)
        log(f"admin: jeton {v} yapıldı")
        return {"ok": True}
    if act == "unlock":
        kinds = b.get("kinds") or ["av", "bg", "frame", "theme"]
        with _lock:
            d = _data()
            d["owned"] = sorted(set(d.get("owned", [])) | {c["id"] for c in CATALOG if c["k"] in kinds})
            _save(d)
        log("admin: eşyalar ücretsiz açıldı")
        return {"ok": True}
    if act == "reset":
        with _lock:
            d = _data()
            d["owned"], d["coins"] = [], START_COINS
            d["prof"] = {**prof_get(d)[0], "av": "av_oyuncu", "frame": "fr_yok", "bg": "bg_gece"}
            _save(d)
        return {"ok": True}
    if act == "db":
        try:
            chat_register()
        except Exception:
            pass
        users = db_users()
        known = {u["code"] for u in users}
        d = chat_load()
        for c, f in d.get("friends", {}).items():
            if c not in known:
                users.append({"code": c, "name": f.get("name", ""), "prof": f.get("profile"), "game": f.get("game"),
                              "seen": f.get("seen", 0), "old": True, "state": f.get("state")})
        return {"ok": True, "users": users, "catalog": CATALOG}
    if act == "edit":
        code = str(b.get("code", ""))
        if not CODE_RE.fullmatch(code) or not isinstance(b.get("ops"), dict):
            return {"error": "bad"}
        err = admin_command(code, b["ops"])
        return {"error": err} if err else {"ok": True, "local": code == chat_me().get("code")}
    if act == "announce":
        title, text = str(b.get("title", "")).strip()[:80], str(b.get("text", "")).strip()[:500]
        if not title:
            return {"error": "bad"}
        err = announce(title, text)
        return {"error": err} if err else {"ok": True}
    if act == "update":
        err = push_update()
        return {"error": err} if err else {"ok": True, "ver": _data().get("upd_last", {}).get("ver")}
    if act == "assets":
        return {"ok": True, "n": publish_assets()}
    if act == "updinfo":
        return {"ok": True, "ver": APP_VER, "last": _data().get("upd_last")}
    if act == "dbdel":
        with _db_lock:
            c = db()
            try:
                c.execute("DELETE FROM users WHERE code=?", (str(b.get("code", "")),))
                c.commit()
            finally:
                c.close()
        return {"ok": True}
    if act == "users":
        d = chat_load()
        me = chat_me(d)
        users = [{"code": me.get("code", ""), "name": me.get("name", "") or persona(), "me": True, "prof": prof_public()}]
        seen = {me.get("code")}
        for c, f in d.get("friends", {}).items():
            users.append({"code": c, "name": f.get("name", ""), "state": f.get("state"), "prof": f.get("profile"),
                          "game": f.get("game"), "seen": f.get("seen", 0)})
            seen.add(c)
        for r, info in d.get("rooms", {}).items():
            for m in d.get("msgs", {}).get("r:" + r, []):
                c = m.get("from", {}).get("c")
                if c and c not in seen:
                    seen.add(c)
                    users.append({"code": c, "name": m["from"].get("n", ""), "room": info.get("name", r), "prof": None,
                                  "game": m.get("g"), "seen": m.get("ts", 0)})
        return {"ok": True, "users": users}
    return {"error": "bad"}



# ---------- Merkez veritabanı ve yönetici komutları ----------
USERS_DB = os.path.join(_BASE, "steam_remote_users.db")
APP_VER = "2026.10.26.0115"
_db_lock = threading.Lock()


def is_admin():
    return bool(ADMIN_D)


def _em(msg):
    h = hashlib.sha256(msg).digest()
    k = (ADMIN_N.bit_length() + 7) // 8
    return int.from_bytes(b"\x00\x01" + b"\xff" * (k - 35) + b"\x00" + h, "big")


def admin_sign(msg):
    return format(pow(_em(msg), ADMIN_D, ADMIN_N), "x")


def admin_verify(msg, sig):
    try:
        return pow(int(sig, 16), 65537, ADMIN_N) == _em(msg)
    except (ValueError, TypeError):
        return False


def db():
    c = sqlite3.connect(USERS_DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS users (code TEXT PRIMARY KEY, name TEXT, data TEXT, first INTEGER, last INTEGER, "
              "ver TEXT, pending INTEGER DEFAULT 0)")
    return c


def db_upsert(rec):
    code = str(rec.get("code", ""))
    if not CODE_RE.fullmatch(code):
        return
    now = int(time.time())
    with _db_lock:
        c = db()
        try:
            row = c.execute("SELECT first FROM users WHERE code=?", (code,)).fetchone()
            c.execute("INSERT OR REPLACE INTO users (code,name,data,first,last,ver,pending) VALUES (?,?,?,?,?,?,0)",
                      (code, str(rec.get("name", ""))[:30], json.dumps(rec), row[0] if row else now, now,
                       str(rec.get("ver", ""))[:20]))
            c.commit()
        finally:
            c.close()


def db_users():
    with _db_lock:
        c = db()
        try:
            rows = c.execute("SELECT code,name,data,first,last,ver,pending FROM users ORDER BY last DESC").fetchall()
        finally:
            c.close()
    out = []
    for code, name, data, first, last, ver, pending in rows:
        try:
            dd = json.loads(data)
        except ValueError:
            dd = {}
        out.append({"code": code, "name": name, "prof": dd.get("prof"), "coins": dd.get("coins"), "owned": dd.get("owned", []),
                    "game": dd.get("game"), "first": first, "seen": last, "ver": ver, "pending": pending,
                    "apikey": dd.get("apikey", ""), "steamid": dd.get("steamid", ""),
                    "me": code == chat_me().get("code")})
    return out


def my_record():
    pr, coins, owned = prof_get()
    me = chat_me()
    return {"code": me.get("code", ""), "name": pr["name"], "prof": prof_public(), "coins": coins, "owned": owned,
            "game": _cur_game(), "ver": APP_VER, "ts": int(time.time()), "stats": my_stats(),
            "apikey": api_key(), "steamid": login_user()[0]}


def chat_register():
    """Hesabı merkez veritabanına bildirir (yönetici kendi PC'sinde doğrudan yazar)."""
    if STANDALONE:
        return
    me = chat_me().get("code")
    if not me or not ADMIN_CODE:
        return
    rec = my_record()
    if is_admin() and me == ADMIN_CODE:
        db_upsert(rec)
        return
    key = _key("adm", ADMIN_CODE)
    ntfy_publish(_topic("adm", ADMIN_CODE), seal(key, chat_env("reg")), seal_bytes(key, json.dumps(rec).encode("utf-8")))


def chat_sync():
    try:
        chat_publish_profile()
    except Exception as e:
        log(f"profil paylaşımı: {e}")
    try:
        chat_register()
    except Exception as e:
        log(f"kayıt: {e}")


ADMIN_OPS = ("name", "bio", "av", "frame", "bg", "coins", "give", "giveall", "reset")


def apply_admin_ops(ops):
    with _lock:
        d = _data()
        pr, coins, owned = prof_get(d)
        owned = set(d.get("owned", []))
        if ops.get("reset"):
            pr = {**PROF_DEFAULT, "name": pr["name"]}
            owned = set()
            d["coins"] = START_COINS
        if ops.get("giveall"):
            owned |= {c["id"] for c in CATALOG}
        for i in ops.get("give") or []:
            if i in CAT:
                owned.add(i)
        for k, kind in (("av", "av"), ("frame", "frame"), ("bg", "bg")):
            v = ops.get(k)
            if v in CAT and CAT[v]["k"] == kind and not CAT[v].get("custom"):
                owned.add(v)
                pr[k] = v
        if ops.get("name"):
            pr["name"] = str(ops["name"]).strip()[:30]
        if "bio" in ops and ops["bio"] is not None:
            pr["bio"] = str(ops["bio"]).strip()[:120]
        if ops.get("coins") is not None:
            try:
                d["coins"] = max(0, min(10**7, int(ops["coins"])))
            except (TypeError, ValueError):
                pass
        d["prof"], d["owned"] = pr, sorted(owned)
        d["admin_note"] = {"ts": int(time.time()), "what": [k for k in ADMIN_OPS if ops.get(k) not in (None, False, [], "")]}
        _save(d)
    with _chat_lock:
        cd = chat_load()
        if cd.get("me", {}).get("code"):
            cd["me"]["name"] = pr["name"]
            chat_save(cd)


def admin_command(code, ops):
    """Yönetici: bir kullanıcının profiline imzalı değişiklik gönderir."""
    ops = {k: ops[k] for k in ADMIN_OPS if k in ops}
    if code == chat_me().get("code"):
        apply_admin_ops(ops)
        threading.Thread(target=chat_sync, daemon=True).start()
        return ""
    body = json.dumps({"target": code, "ops": ops, "ts": int(time.time()), "nonce": secrets.token_hex(8)},
                      sort_keys=True, ensure_ascii=False)
    env = chat_env("cmd", body=body, sig=admin_sign(body.encode("utf-8")))
    err = ntfy_publish(_topic("cmd", code), seal(_key("cmd", code), env))
    if not err:
        with _db_lock:
            c = db()
            try:
                c.execute("UPDATE users SET pending=? WHERE code=?", (int(time.time()), code))
                c.commit()
            finally:
                c.close()
    return err


def handle_admin_cmd(env):
    body, sig = str(env.get("body", "")), str(env.get("sig", ""))
    if not admin_verify(body.encode("utf-8"), sig):
        log("geçersiz yönetici komutu reddedildi")
        return
    try:
        cmd = json.loads(body)
    except ValueError:
        return
    if cmd.get("target") != chat_me().get("code") or abs(time.time() - int(cmd.get("ts", 0))) > 2 * 86400:
        return
    with _lock:
        d = _data()
        seen = d.get("cmd_seen", [])
        if cmd.get("nonce") in seen:
            return
        d["cmd_seen"] = (seen + [cmd.get("nonce")])[-100:]
        _save(d)
    apply_admin_ops(cmd.get("ops") or {})
    log("yönetici profil değişikliği uygulandı")
    notify("chat", "Yönetici", "Profilin yönetici tarafından güncellendi.")
    threading.Thread(target=chat_sync, daemon=True).start()



# ---------- Jeton ekonomisi, görevler, rozetler, çark, liderlik ----------
def add_coins(amt, why, d=None):
    """Jeton ekler (d verilirse kaydetmez, çağıran kaydeder)."""
    own = d is None
    if own:
        _lock.acquire()
        d = _data()
    try:
        d["coins"] = max(0, d.get("coins", START_COINS) + int(amt))
        d["ledger"] = (d.get("ledger", []) + [{"ts": int(time.time()), "amt": int(amt), "why": why}])[-60:]
        if own:
            _save(d)
    finally:
        if own:
            _lock.release()
    return d["coins"]


def today_key(t=None):
    return time.strftime("%Y%m%d", time.localtime(t or time.time()))


def daily_login():
    """Günün ilk girişinde jeton verir; arka arkaya günlerde bonus artar."""
    with _lock:
        d = _data()
        tk = today_key()
        if d.get("login_day") == tk:
            return None
        yk = today_key(time.time() - 86400)
        d["login_streak"] = d.get("login_streak", 0) + 1 if d.get("login_day") == yk else 1
        d["login_day"] = tk
        amt = 20 + 5 * min(d["login_streak"] - 1, 6)
        add_coins(amt, f"Günlük giriş ({d['login_streak']}. gün)", d)
        _save(d)
    return amt


_earn = {"acc": 0, "t": 0}


def earn_tick(playing):
    """Oyun oynarken her tam saat için 10 jeton."""
    now = time.time()
    if playing and _earn["t"]:
        _earn["acc"] += min(10, now - _earn["t"])
        if _earn["acc"] >= 3600:
            _earn["acc"] -= 3600
            add_coins(10, "1 saat oyun")
    _earn["t"] = now


QUESTS = {
    "play60": ("Bugün 1 saat oyna", 30),
    "play2": ("Bugün 2 farklı oyun oyna", 30),
    "chat3": ("Sohbette 3 mesaj gönder", 20),
    "invite": ("Bir arkadaşını oyuna davet et", 25),
    "spin": ("Şans çarkını çevir", 10),
    "fav": ("Favori oyununu oyna", 25),
    "remote": ("Telefondan bir oyun başlat", 15),
    "react": ("Bir mesaja tepki bırak", 10),
}


def _today_sessions():
    mid = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    sess = [tuple(x) for x in _data().get("sessions", [])]
    if _state["app"] != "0" and _state["since"]:
        sess.append((_state["app"], _state["since"], time.time()))
    return [(a, max(s0, mid), e) for a, s0, e in sess if e > mid]


def quest_progress(qid, d):
    tk = today_key()
    ts = _today_sessions()
    if qid == "play60":
        return min(60, round(sum(e - s0 for _, s0, e in ts) / 60)), 60
    if qid == "play2":
        return min(2, len({a for a, s0, e in ts if e - s0 >= 300})), 2
    if qid == "fav":
        fav = d.get("prof", {}).get("fav")
        return (1 if fav and any(a == fav for a, _, _ in ts) else 0), 1
    if qid in ("spin", "remote"):
        return (1 if d.get(f"q_{qid}") == tk else 0), 1
    cd = chat_load()
    me = chat_me(cd).get("code")
    mid = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    mine = [m for lst in cd.get("msgs", {}).values() for m in lst if m.get("from", {}).get("c") == me and m.get("ts", 0) >= mid]
    if qid == "chat3":
        return min(3, sum(1 for m in mine if m.get("t") in ("msg", "voice", "img"))), 3
    if qid == "invite":
        return (1 if any(m.get("t") in ("invite", "event") for m in mine) else 0), 1
    if qid == "react":
        return (1 if d.get("q_react") == tk else 0), 1
    return 0, 1


def quest_mark(qid):
    with _lock:
        d = _data()
        d[f"q_{qid}"] = today_key()
        _save(d)


def quests():
    d = _data()
    tk = today_key()
    q = d.get("quests", {})
    if q.get("day") != tk:
        rnd = random.Random(tk + (chat_me().get("code") or "x"))
        q = {"day": tk, "list": rnd.sample(sorted(QUESTS), 3), "claimed": []}
        with _lock:
            dd = _data()
            dd["quests"] = q
            _save(dd)
    out = []
    for qid in q["list"]:
        cur, need = quest_progress(qid, d)
        out.append({"id": qid, "t": QUESTS[qid][0], "r": QUESTS[qid][1], "cur": cur, "need": need,
                    "done": cur >= need, "claimed": qid in q["claimed"]})
    return out


def quest_claim(qid):
    for q in quests():
        if q["id"] == qid and q["done"] and not q["claimed"]:
            with _lock:
                d = _data()
                d["quests"]["claimed"].append(qid)
                d["q_total"] = d.get("q_total", 0) + 1
                add_coins(q["r"], "Görev: " + q["t"], d)
                _save(d)
            check_badges()
            return {"ok": True, "coins": _data().get("coins")}
    return {"error": "quest"}


BADGES = {
    "ilk": ("🎯", "İlk görev", "İlk günlük görevini tamamla"),
    "gorev20": ("🏹", "Görev avcısı", "20 görev tamamla"),
    "sadik": ("📅", "Sadık", "7 gün üst üste gir"),
    "gece": ("🦉", "Gece kuşu", "Gece 02:00–05:00 arası oyna"),
    "maraton": ("🏃", "Maratoncu", "Tek seferde 4 saat oyna"),
    "sosyal": ("🦋", "Sosyal kelebek", "5 arkadaş edin"),
    "koleksiyon": ("🧩", "Koleksiyoncu", "Mağazadan 15 eşyaya sahip ol"),
    "zengin": ("💰", "Zengin", "5.000 jetona ulaş"),
    "sansli": ("🍀", "Şanslı", "Çarkta büyük ödülü kazan"),
    "seviye5": ("⭐", "Usta oyuncu", "Seviye 5'e ulaş"),
    "davetci": ("📣", "Organizatör", "Bir oyun gecesi planla"),
}


def check_badges():
    d = _data()
    have = d.get("badges", {})
    sess = d.get("sessions", [])
    friends = [f for f in chat_load().get("friends", {}).values() if f.get("state") == "ok"]
    owned = [i for i in d.get("owned", []) if CAT.get(i, {}).get("p", 0) > 0]
    cond = {
        "ilk": d.get("q_total", 0) >= 1, "gorev20": d.get("q_total", 0) >= 20,
        "sadik": d.get("login_streak", 0) >= 7,
        "gece": any(2 <= time.localtime(st).tm_hour < 5 or 2 <= time.localtime(en).tm_hour < 5 for _, st, en in sess),
        "maraton": any(en - st >= 4 * 3600 for _, st, en in sess),
        "sosyal": len(friends) >= 5, "koleksiyon": len(owned) >= 15,
        "zengin": d.get("coins", START_COINS) >= 5000, "sansli": d.get("jackpot", 0) >= 1,
        "seviye5": 1 + total_minutes() // 300 >= 5, "davetci": d.get("q_event", 0) >= 1,
    }
    new = [b for b, ok in cond.items() if ok and b not in have]
    if new:
        with _lock:
            dd = _data()
            bs = dd.setdefault("badges", {})
            for b in new:
                bs[b] = int(time.time())
                add_coins(50, "Rozet: " + BADGES[b][1], dd)
            _save(dd)
        for b in new:
            notify("chat", "Yeni rozet: " + BADGES[b][1], BADGES[b][2] + " · +50 jeton")
    return new


WHEEL = [("10", 10, 20), ("20", 20, 18), ("30", 30, 15), ("50", 50, 13), ("75", 75, 10), ("100", 100, 8),
         ("Eşya", 0, 7), ("200", 200, 6), ("500", 500, 3)]
SPIN_COST = 50


def wheel_state():
    d = _data()
    return {"free": d.get("spin_day") != today_key(), "cost": SPIN_COST, "seg": [w[0] for w in WHEEL]}


def wheel_spin():
    with _lock:
        d = _data()
        free = d.get("spin_day") != today_key()
        if not free:
            if d.get("coins", START_COINS) < SPIN_COST:
                return {"error": "coins"}
            add_coins(-SPIN_COST, "Çark çevirme", d)
        d["spin_day"] = today_key()
        d["q_spin"] = today_key()
        idx = random.choices(range(len(WHEEL)), weights=[w[2] for w in WHEEL])[0]
        label, amt, _ = WHEEL[idx]
        prize = {"label": label}
        if label == "Eşya":
            owned = set(d.get("owned", []))
            pool = [c for c in CATALOG if c["p"] >= 150 and c["id"] not in owned]
            if pool:
                it = random.choice(pool)
                d["owned"] = sorted(owned | {it["id"]})
                prize["item"] = {"id": it["id"], "n": it["n"], "k": it["k"]}
            else:
                amt = 300
        if amt:
            add_coins(amt, "Şans çarkı", d)
            prize["coins"] = amt
        if label == "500":
            d["jackpot"] = d.get("jackpot", 0) + 1
        _save(d)
    check_badges()
    return {"ok": True, "idx": idx, "prize": prize, "coins": _data().get("coins")}


def my_stats():
    d = _data()
    act = activity()
    mins = total_minutes()
    return {"total": mins, "week": act["total"], "level": 1 + mins // 300, "coins": d.get("coins", START_COINS),
            "badges": sorted(d.get("badges", {})), "quests": d.get("q_total", 0)}


BOARD_CATS = (("week", "Bu hafta en çok oynayan", "dk"), ("level", "En yüksek seviye", "Sv"),
              ("coins", "En zengin", "🪙"), ("badges", "En çok rozet", "🏅"))


def board_compute():
    try:
        chat_register()  # yöneticinin kendi kaydı da güncel olsun
    except Exception:
        pass
    rows = []
    for u in db_users():
        st = (json.loads(json.dumps(u)) or {})
        rec_stats = {}
        try:
            with _db_lock:
                c = db()
                try:
                    row = c.execute("SELECT data FROM users WHERE code=?", (u["code"],)).fetchone()
                finally:
                    c.close()
            rec_stats = json.loads(row[0]).get("stats", {}) if row else {}
        except Exception:
            pass
        p = u.get("prof") or {}
        rows.append({"code": u["code"], "name": p.get("name") or u.get("name"), "prof": {k: p.get(k) for k in ("av", "frame", "photo")},
                     "week": rec_stats.get("week", 0), "level": rec_stats.get("level", p.get("level", 1)),
                     "coins": rec_stats.get("coins", u.get("coins") or 0), "badges": len(rec_stats.get("badges", []))})
    board = {"t": int(time.time()), "n": len(rows), "cats": []}
    for key, title, unit in BOARD_CATS:
        top = sorted(rows, key=lambda r: -(r.get(key) or 0))[:10]
        board["cats"].append({"key": key, "title": title, "unit": unit,
                              "rows": [{"code": r["code"], "name": r["name"], "prof": r["prof"], "v": r.get(key) or 0} for r in top]})
    return board


def board_publish_loop():
    """Yönetici: liderlik tablosunu herkese dağıtır (değiştiyse, en fazla 30 dk'da bir)."""
    last = ""
    while True:
        time.sleep(60)
        try:
            if not (is_admin() and chat_me().get("code") == ADMIN_CODE):
                continue
            b = board_compute()
            sig = hashlib.sha256(json.dumps(b["cats"], sort_keys=True).encode()).hexdigest()
            if sig != last:
                key = _key("board", ADMIN_CODE)
                if not ntfy_publish(_topic("board", ADMIN_CODE), seal(key, chat_env("board")),
                                    seal_bytes(key, json.dumps(b).encode("utf-8"))):
                    last = sig
            time.sleep(1740)
        except Exception as e:
            log(f"liderlik tablosu: {e}")


def board_get():
    if is_admin() and chat_me().get("code") == ADMIN_CODE:
        return board_compute()
    return chat_load().get("board") or {"cats": [], "n": 0}



# ---------- Oyun gecesi hatırlatmaları, Özet panoları ----------
def upcoming_events():
    d = chat_load()
    me = chat_me(d).get("code")
    now = time.time()
    out = []
    for cid, lst in d.get("msgs", {}).items():
        for m in lst:
            if m.get("t") == "event" and (m.get("when") or 0) > now - 2 * 3600:
                name = (d.get("rooms", {}).get(cid[2:], {}).get("name") if cid.startswith("r:")
                        else d.get("friends", {}).get(cid[2:], {}).get("name")) or "Sohbet"
                rs = m.get("rsvp", {})
                out.append({"cid": cid, "conv": name, "mid": m.get("mid"), "title": m.get("title"), "when": m.get("when"),
                            "game": m.get("game"), "mine": (rs.get(me) or {}).get("a"),
                            "yes": sum(1 for r in rs.values() if r.get("a") == "yes")})
    return sorted(out, key=lambda e: e["when"])[:10]


def event_reminder_loop():
    while True:
        time.sleep(60)
        try:
            d = chat_load()
            if not chat_me(d).get("code"):
                continue
            now = time.time()
            done = set(d.get("reminded", []))
            new = []
            for e in upcoming_events():
                if e["mine"] == "no":
                    continue
                for stage, at, txt in (("15", e["when"] - 900, "15 dakika sonra başlıyor"), ("0", e["when"], "Şimdi başlıyor!")):
                    k = f"{e['mid']}:{stage}"
                    if at <= now < at + 600 and k not in done:
                        new.append(k)
                        notify("chat", f"📅 {e['title']}", f"{txt} · {e['conv']}" + (f" · {e['game']['name']}" if e.get("game") else ""),
                               (e.get("game") or {}).get("appid"))
            if new:
                with _chat_lock:
                    cd = chat_load()
                    cd["reminded"] = (cd.get("reminded", []) + new)[-300:]
                    chat_save(cd)
        except Exception as e:
            log(f"etkinlik hatırlatma: {e}")


def friends_now():
    d = chat_load()
    out = []
    for c, f in d.get("friends", {}).items():
        if f.get("state") != "ok":
            continue
        fresh = f.get("seen", 0) > time.time() - 4 * 3600
        g = f.get("game") if fresh else None
        out.append({"code": c, "name": f.get("name") or c, "prof": f.get("profile"), "game": g,
                    "mins": round((time.time() - f["gs"]) / 60) if g and f.get("gs") else None, "seen": f.get("seen", 0)})
    return sorted(out, key=lambda x: (not x["game"], -(x["seen"] or 0)))



# ---------- Otomatik güncelleme ve duyurular ----------
def friend_build(src, ver):
    """Yönetici dosyasından arkadaş kopyasını üretir (gizli anahtar ve PIN çıkarılır)."""
    out = re.sub(r'(?m)^APP_VER = ".*"$', f'APP_VER = "{ver}"', src)
    out = re.sub(r"(?m)^ADMIN_D = \d+.*$", "ADMIN_D = 0  # bu kopya yönetici değil", out)
    out = re.sub(r'(?m)^PIN = ".*$', 'PIN = "1234"  # <-- BUNU DEĞİŞTİR (ilk girişten sonra Ayarlar > PIN\'den de değiştirebilirsin)', out)
    assert "ADMIN_D = 0" in out
    return out


def push_update(republish=False):
    me = os.path.abspath(__file__)
    src = open(me, encoding="utf-8").read()
    d = _data()
    if republish:
        ver = d.get("upd_last", {}).get("ver")
        if not ver:
            return "none"
    else:
        ver = time.strftime("%Y.%m.%d.%H%M")
        src2 = re.sub(r'(?m)^APP_VER = ".*"$', f'APP_VER = "{ver}"', src)
        if src2 != src:
            with open(me, "w", encoding="utf-8") as f:
                f.write(src2)
            src = src2
    data = friend_build(src, ver).encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    key = _key("upd", ADMIN_CODE)
    env = chat_env("update", ver=ver, sha=sha, sig=admin_sign(f"{sha}|{ver}".encode()))
    err = ntfy_publish(_topic("upd", ADMIN_CODE), seal(key, env), seal_bytes(key, data))
    if not err:
        with _lock:
            dd = _data()
            dd["upd_last"] = {"ver": ver, "ts": int(time.time()) if not republish else dd.get("upd_last", {}).get("ts", int(time.time())),
                              "pub": int(time.time()), "size": len(data)}
            _save(dd)
        log(f"güncelleme yayınlandı: {ver} ({len(data)} bayt)")
        threading.Thread(target=publish_assets, daemon=True).start()
    return err


def update_republish_loop():
    while True:
        time.sleep(1800)
        try:
            u = _data().get("upd_last") or {}
            if is_admin() and u.get("ver") and time.time() - u.get("ts", 0) < 7 * 86400 and time.time() - u.get("pub", 0) > 20 * 3600:
                push_update(republish=True)
        except Exception as e:
            log(f"güncelleme yeniden yayını: {e}")


def apply_update(env, data):
    ver, sha, sig = str(env.get("ver", "")), str(env.get("sha", "")), str(env.get("sig", ""))
    if is_admin() or not re.fullmatch(r"\d{4}\.\d\d\.\d\d\.\d{4}", ver) or ver <= APP_VER:
        return
    if hashlib.sha256(data).hexdigest() != sha or not admin_verify(f"{sha}|{ver}".encode(), sig):
        log("imzası geçersiz güncelleme reddedildi")
        return
    me = os.path.abspath(__file__)
    try:
        cur = open(me, encoding="utf-8").read()
        new = data.decode("utf-8")
        for pat in (r'(?m)^PIN = ".*$', r'(?m)^STEAM_API_KEY = ".*$'):
            m = re.search(pat, cur)
            if m:
                new = re.sub(pat, lambda _: m.group(0), new, count=1)
        compile(new, me, "exec")
        with open(os.path.join(os.path.dirname(me), "steam_remote_onceki.py"), "w", encoding="utf-8") as f:
            f.write(cur)
        with open(me, "w", encoding="utf-8") as f:
            f.write(new)
    except Exception as e:
        log(f"güncelleme uygulanamadı: {e}")
        return
    log(f"program {APP_VER} -> {ver} güncellendi, yeniden başlatılıyor")
    notify("chat", "Program güncellendi", f"Yeni sürüm ({ver}) yüklendi, sunucu yeniden başlıyor.")
    time.sleep(2)
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and os.path.exists(pyw):
        exe = pyw
    try:
        subprocess.Popen([exe, me, "--wait"], creationflags=(0x00000008 | 0x00000200 | NO_WINDOW) if os.name == "nt" else 0,
                         close_fds=True, cwd=os.path.dirname(me))
    except Exception as e:
        log(f"yeniden başlatılamadı: {e}")
        return
    os._exit(0)


def announce(title, text):
    body = json.dumps({"title": title, "text": text, "ts": int(time.time()), "id": secrets.token_hex(6)}, ensure_ascii=False)
    env = chat_env("ann", body=body, sig=admin_sign(body.encode("utf-8")))
    err = ntfy_publish(_topic("ann", ADMIN_CODE), seal(_key("ann", ADMIN_CODE), env))
    if not err:
        store_ann(json.loads(body))
    return err


def store_ann(a):
    with _lock:
        d = _data()
        anns = d.get("anns", [])
        if any(x.get("id") == a.get("id") for x in anns):
            return False
        d["anns"] = (anns + [{k: a.get(k) for k in ("id", "title", "text", "ts")}])[-20:]
        _save(d)
    return True


# ---------- Misafir girişi ----------
GUEST_GET = {"/api/games", "/api/status", "/api/whoami"}
GUEST_POST = {"/api/launch", "/api/stop"}


def guest_pin():
    return str(_data().get("guest_pin") or "")


# ---------- Adres & QR ----------
def addresses():
    lan = local_ip()
    ts = tailscale_ip()
    return {"lan": f"http://{lan}:{PORT}", "ts": f"http://{ts}:{PORT}" if ts else "", "https": f"https://{lan}:{PORT + 1}"}


QR_PAGE = r"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oyunlarım Remote — Bağlan</title><style>
*{box-sizing:border-box}body{margin:0;background:#1b2838;color:#e6edf5;font-family:system-ui,sans-serif;min-height:100vh;padding:28px 16px}
.wrap{max-width:760px;margin:0 auto}h1{font-size:24px;margin:0 0 4px;text-align:center}.sub{color:#8fa3b8;text-align:center;margin:0 auto 22px;max-width:520px;line-height:1.5;font-size:14px}
.card{background:#223247;border-radius:16px;padding:20px;margin:0 0 16px}
.steps{counter-reset:s;list-style:none;margin:0;padding:0}.steps li{counter-increment:s;position:relative;padding:8px 0 8px 40px;line-height:1.5}
.steps li::before{content:counter(s);position:absolute;left:0;top:6px;width:26px;height:26px;border-radius:50%;background:#66c0f4;color:#0b1a28;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:14px}
.qrs{display:flex;flex-wrap:wrap;gap:16px;justify-content:center}
.qc{background:#1b2838;border:1px solid #2f4560;border-radius:14px;padding:18px;text-align:center;flex:1;min-width:240px;max-width:320px}
.qc h3{margin:0 0 4px;font-size:16px}.qc p{margin:0 0 12px;color:#8fa3b8;font-size:13px}
.q{background:#fff;padding:10px;border-radius:10px;display:inline-block}.u{margin-top:12px;font-family:monospace;font-size:15px;word-break:break-all;background:#0e1a27;padding:8px 10px;border-radius:8px}
.warn{background:linear-gradient(135deg,rgba(255,196,107,.18),rgba(255,138,128,.12));border:1px solid #e8b44a;border-radius:12px;padding:14px 16px;color:#ffd98a;font-size:14px;line-height:1.5}
.ok{color:#5ad17a}.foot{text-align:center;color:#6f8399;font-size:13px;margin-top:8px}
</style></head><body><div class="wrap">
<h1><span class="ok">●</span> Oyunlarım Remote çalışıyor</h1>
<p class="sub">Program PC'nde <b>arka planda</b> çalışıyor. Bu pencereyi kapatabilirsin — sunucu kapanmaz. Telefonundan bağlanmak için:</p>
<div id="warn"></div>
<div class="card"><ol class="steps">
<li>Telefonunu bu bilgisayarla <b>aynı Wi-Fi</b> ağına bağla.</li>
<li>Aşağıdaki <b>QR kodu</b> telefon kamerasıyla okut (ya da adresi telefonun tarayıcısına yaz).</li>
<li>Açılan sayfada <b>PIN</b>'i gir. İlk girişten sonra <b>Ayarlar → PIN</b>'den kendi PIN'ini belirle.</li>
</ol></div>
<div class="card"><div id="o" class="qrs">Yükleniyor…</div></div>
<p class="foot">Kapatmak istersen: telefonda <b>Ayarlar → Sunucuyu kapat</b>. Tekrar açmak için uygulamaya yeniden çift tıkla.</p>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js"></script>
<script>fetch("/qr.json").then(r=>r.json()).then(a=>{
if(a.pin_default)document.getElementById("warn").innerHTML='<div class="card warn">⚠️ PIN şu an varsayılan: <b>1234</b>. Telefondan girdikten sonra <b>Ayarlar → PIN</b>\'den mutlaka değiştir; yoksa aynı ağdaki başkaları da bağlanabilir.</div>';
const L=[["Evde (Wi-Fi)",a.lan,"Telefon aynı Wi-Fi'dayken"],["Dışarıda (Tailscale)",a.ts,"Telefonda Tailscale açıkken"]].filter(x=>x[1]);
document.getElementById("o").outerHTML='<div id="o" class="qrs">'+L.map(([t,u,n])=>{let s="";try{const q=qrcode(0,"M");q.addData(u);q.make();s=q.createSvgTag({cellSize:6,margin:0})}catch(e){s="(QR için internet gerekli — adresi elle yaz)"}return `<div class="qc"><h3>${t}</h3><p>${n}</p><div class="q">${s}</div><div class="u">${u}</div></div>`}).join("")+'</div>';
}).catch(e=>{document.getElementById("o").textContent="Adres alınamadı."})</script>
</body></html>"""



# ---------- Görsel varlıklar (hareketli arka planlar, kendi arka planın) ----------
ASSETS = os.path.join(_BASE, "steam_remote_assets")
ASSET_RE = re.compile(r"(bg_[a-z0-9_]+|c_[a-f0-9]{12})\.(gif|jpg|png|webp)")


def _seed_bundled_assets():
    try:
        os.makedirs(ASSETS, exist_ok=True)
        base = sys._MEIPASS if getattr(sys, "frozen", False) else _BASE
        src = os.path.join(base, "steam_remote_assets")
        if src != ASSETS and os.path.isdir(src):
            for fn in os.listdir(src):
                dst = os.path.join(ASSETS, fn)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(os.path.join(src, fn), dst)
                    except Exception:
                        pass
    except Exception:
        pass


_seed_bundled_assets()
ASSET_CT = {"gif": "image/gif", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def img_ext(data):
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def asset_save(name, data):
    if not ASSET_RE.fullmatch(name):
        return False
    os.makedirs(ASSETS, exist_ok=True)
    path = os.path.join(ASSETS, name)
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return True


def custom_bg_upload(data):
    pr, coins, owned = prof_get()
    if "bg_custom" not in owned:
        return {"error": "owned"}
    ext = img_ext(data)
    if not ext:
        return {"error": "type"}
    name = f"c_{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
    asset_save(name, data)
    with _lock:
        d = _data()
        pr = {**PROF_DEFAULT, **d.get("prof", {})}
        pr["bgf"], pr["bg"] = name, "bg_custom"
        d["prof"] = pr
        _save(d)
    threading.Thread(target=publish_custom_bg, args=(name, data), daemon=True).start()
    return {"ok": True, "f": name}


def publish_custom_bg(name, data):
    """Kendi arka planını arkadaşlara ve merkeze gönderir, sonra profili paylaşır."""
    code = chat_me().get("code")
    if code:
        env = chat_env("bgimg", f=name, sha=hashlib.sha256(data).hexdigest())
        ntfy_publish(_topic("pr", code), seal(_key("pr", code), env), seal_bytes(_key("pr", code), data))
        if ADMIN_CODE and not (is_admin() and code == ADMIN_CODE):
            ntfy_publish(_topic("adm", ADMIN_CODE), seal(_key("adm", ADMIN_CODE), env), seal_bytes(_key("adm", ADMIN_CODE), data))
    chat_sync()


def receive_bgimg(env, ev, key):
    name, sha = str(env.get("f", "")), str(env.get("sha", ""))
    att = ev.get("attachment") or {}
    if not re.fullmatch(r"c_[a-f0-9]{12}\.(gif|jpg|png|webp)", name) or os.path.exists(os.path.join(ASSETS, name)):
        return
    if not att.get("url", "").startswith(NTFY + "/") or int(att.get("size") or 0) > 3 * 2**20:
        return
    try:
        with urllib.request.urlopen(att["url"], timeout=60) as r:
            raw = open_bytes(key, r.read(3 * 2**20 + 64))
        if raw and hashlib.sha256(raw).hexdigest() == sha and sha[:12] == name[2:14]:
            asset_save(name, raw)
    except Exception as e:
        log(f"arka plan alınamadı: {e}")


def publish_assets():
    """Yönetici: mağazadaki hareketli arka planları herkese dağıtır (imzalı)."""
    sent = 0
    key = _key("asset", ADMIN_CODE)
    for c in CATALOG:
        f = c.get("img")
        path = os.path.join(ASSETS, f) if f else ""
        if not f or not os.path.exists(path):
            continue
        data = open(path, "rb").read()
        sha = hashlib.sha256(data).hexdigest()
        env = chat_env("asset", f=f, sha=sha, sig=admin_sign(f"{f}|{sha}".encode()))
        if not ntfy_publish(_topic("asset", ADMIN_CODE), seal(key, env), seal_bytes(key, data)):
            sent += 1
    log(f"{sent} mağaza görseli dağıtıldı")
    return sent


def receive_asset(env, ev, key):
    name, sha, sig = str(env.get("f", "")), str(env.get("sha", "")), str(env.get("sig", ""))
    att = ev.get("attachment") or {}
    if not re.fullmatch(r"bg_[a-z0-9_]+\.(gif|jpg|png|webp)", name) or not admin_verify(f"{name}|{sha}".encode(), sig):
        return
    path = os.path.join(ASSETS, name)
    if os.path.exists(path) and hashlib.sha256(open(path, "rb").read()).hexdigest() == sha:
        return
    try:
        with urllib.request.urlopen(att["url"], timeout=60) as r:
            raw = open_bytes(key, r.read(3 * 2**20 + 64))
        if raw and hashlib.sha256(raw).hexdigest() == sha:
            if os.path.exists(path):
                os.remove(path)
            asset_save(name, raw)
            log(f"mağaza görseli alındı: {name}")
    except Exception as e:
        log(f"mağaza görseli alınamadı: {e}")



# ---------- Çalan medya (Windows SMTC) ----------
_media = {"t": 0, "data": None}
_MEDIA_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$ext=[System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}
function Await($t,$ty){$m=$ext[0].MakeGenericMethod($ty);$nt=$m.Invoke($null,@($t));$nt.Wait(-1)|Out-Null;$nt.Result}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]|Out-Null
try{
 $mgr=Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
 $s=$mgr.GetCurrentSession()
 if($s){$i=Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  $pb=$s.GetPlaybackInfo()
  [pscustomobject]@{title=$i.Title;artist=$i.Artist;album=$i.AlbumTitle;app=$s.SourceAppUserModelId;status=[string]$pb.PlaybackStatus}|ConvertTo-Json -Compress}
}catch{}
"""


def media_info():
    if time.time() - _media["t"] < 3:
        return _media["data"]
    _media["t"] = time.time()
    data = None
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _MEDIA_PS], capture_output=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.decode("utf-8", "ignore").strip()
        if out:
            j = json.loads(out)
            title = (j.get("title") or "").strip()
            if title:
                app = (j.get("app") or "").lower()
                src = "Spotify" if "spotify" in app else "YouTube" if "chrome" in app or "edge" in app or "firefox" in app else ""
                data = {"title": title, "artist": (j.get("artist") or "").strip(), "album": (j.get("album") or "").strip(),
                        "playing": j.get("status") == "Playing", "src": src}
    except Exception as e:
        log(f"medya bilgisi: {e}")
    _media["data"] = data
    return data


# ---------- Ses cihazları ve uygulama sesleri (WASAPI / COM) ----------
def _co_init():
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass


def _guid(g):
    import uuid
    u = uuid.UUID(g)
    b = u.bytes_le
    return (ctypes.c_byte * 16)(*b)


def _vt(ptr, idx, res, args):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fp = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[idx]
    return ctypes.CFUNCTYPE(res, ctypes.c_void_p, *args)(fp)


def _release(ptr):
    if ptr:
        try:
            _vt(ptr, 2, ctypes.c_ulong, [])(ptr)
        except Exception:
            pass


CLSID_MMDE = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_MMDE = "A95664D2-9614-4F35-A746-DE8DB63617E6"
CLSID_PCFG = "870AF99C-171D-4F9E-AF0D-E63DF40C2BC9"
IID_PCFG = "F8679F50-850A-41CF-9C72-430F290290C8"
IID_ASM2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_SAV = "87CE5498-68D6-44E5-9215-6DA47EF883D8"
IID_ASC2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
# PKEY_Device_FriendlyName
_PKEY_FN = (b"\xa4\x5c\x25\x4e\xdf\x95\x5f\x43\xb3\x9a\xa9\x53\x53\xb8\x9d\x29", 14)


def _enum():
    _co_init()
    pp = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(_guid(CLSID_MMDE), None, 1, _guid(IID_MMDE), ctypes.byref(pp))
    return pp if hr == 0 and pp.value else None


def _dev_name(dev):
    try:
        ps = ctypes.c_void_p()
        if _vt(dev, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.c_void_p])(dev, 0, ctypes.byref(ps)) != 0:
            return ""
        pk = (ctypes.c_byte * 20)(*_PKEY_FN[0], *(0, 0, 0, 0), *_PKEY_FN[1].to_bytes(4, "little"))
        var = (ctypes.c_byte * 24)()
        if _vt(ps, 5, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(ps, ctypes.byref(pk), ctypes.byref(var)) != 0:
            _release(ps)
            return ""
        ptr = ctypes.cast(ctypes.byref(var, 8), ctypes.POINTER(ctypes.c_wchar_p))[0]
        name = ptr or ""
        try:
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(var))
        except Exception:
            pass
        _release(ps)
        return name
    except Exception:
        return ""


def audio_devices():
    enum = _enum()
    if not enum:
        return {"error": "com"}
    out = []
    try:
        defid = ""
        d = ctypes.c_void_p()
        if _vt(enum, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p])(enum, 0, 1, ctypes.byref(d)) == 0 and d.value:
            pid = ctypes.c_wchar_p()
            if _vt(d, 5, ctypes.c_long, [ctypes.c_void_p])(d, ctypes.byref(pid)) == 0:
                defid = pid.value or ""
            _release(d)
        coll = ctypes.c_void_p()
        if _vt(enum, 3, ctypes.c_long, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p])(enum, 0, 1, ctypes.byref(coll)) != 0:
            return {"error": "enum"}
        cnt = ctypes.c_uint32()
        _vt(coll, 3, ctypes.c_long, [ctypes.c_void_p])(coll, ctypes.byref(cnt))
        for i in range(cnt.value):
            dev = ctypes.c_void_p()
            if _vt(coll, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.c_void_p])(coll, i, ctypes.byref(dev)) != 0:
                continue
            did = ctypes.c_wchar_p()
            _vt(dev, 5, ctypes.c_long, [ctypes.c_void_p])(dev, ctypes.byref(did))
            out.append({"id": did.value or "", "name": _dev_name(dev) or "Ses cihazı", "cur": (did.value or "") == defid})
            _release(dev)
        _release(coll)
    except Exception as e:
        log(f"ses cihazları: {e}")
        return {"error": "com"}
    finally:
        _release(enum)
    return {"devices": out}


def set_audio_device(devid):
    _co_init()
    pp = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(_guid(CLSID_PCFG), None, 1, _guid(IID_PCFG), ctypes.byref(pp))
    if hr != 0 or not pp.value:
        return False
    try:
        ok = True
        for role in (0, 1, 2):
            r = _vt(pp, 13, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int])(pp, devid, role)
            ok = ok and (r == 0)
        return ok
    except Exception as e:
        log(f"ses cihazı ayarlama: {e}")
        return False
    finally:
        _release(pp)


def app_volumes():
    enum = _enum()
    if not enum:
        return {"error": "com"}
    out = []
    dev = mgr = en = None
    try:
        dev = ctypes.c_void_p()
        if _vt(enum, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p])(enum, 0, 1, ctypes.byref(dev)) != 0 or not dev.value:
            return {"apps": []}
        mgr = ctypes.c_void_p()
        if _vt(dev, 3, ctypes.c_long, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p])(dev, _guid(IID_ASM2), 23, None, ctypes.byref(mgr)) != 0:
            return {"apps": []}
        en = ctypes.c_void_p()
        if _vt(mgr, 5, ctypes.c_long, [ctypes.c_void_p])(mgr, ctypes.byref(en)) != 0:
            return {"apps": []}
        cnt = ctypes.c_int()
        _vt(en, 3, ctypes.c_long, [ctypes.c_void_p])(en, ctypes.byref(cnt))
        names = {p.get("ProcessId"): (p.get("ExecutablePath") or "") for p in processes()}
        for i in range(cnt.value):
            ctl = ctypes.c_void_p()
            if _vt(en, 4, ctypes.c_long, [ctypes.c_int, ctypes.c_void_p])(en, i, ctypes.byref(ctl)) != 0:
                continue
            c2 = ctypes.c_void_p()
            pid = 0
            if _vt(ctl, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(ctl, _guid(IID_ASC2), ctypes.byref(c2)) == 0:
                pv = ctypes.c_uint32()
                _vt(c2, 14, ctypes.c_long, [ctypes.c_void_p])(c2, ctypes.byref(pv))
                pid = pv.value
                _release(c2)
            sv = ctypes.c_void_p()
            if pid and _vt(ctl, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(ctl, _guid(IID_SAV), ctypes.byref(sv)) == 0:
                vol = ctypes.c_float()
                mute = ctypes.c_int()
                _vt(sv, 4, ctypes.c_long, [ctypes.c_void_p])(sv, ctypes.byref(vol))
                _vt(sv, 6, ctypes.c_long, [ctypes.c_void_p])(sv, ctypes.byref(mute))
                exe = os.path.splitext(os.path.basename(names.get(pid, "") or ""))[0] or f"PID {pid}"
                if exe.lower() not in ("", "audiodg"):
                    out.append({"pid": pid, "name": exe, "vol": round(vol.value * 100), "mute": bool(mute.value)})
                _release(sv)
            _release(ctl)
    except Exception as e:
        log(f"uygulama sesleri: {e}")
        return {"error": "com"}
    finally:
        _release(en)
        _release(mgr)
        _release(dev)
        _release(enum)
    seen = {}
    for a in out:
        seen[a["name"]] = seen.get(a["name"]) or a
    return {"apps": sorted(seen.values(), key=lambda a: a["name"].lower())}


def set_app_volume(pid, vol=None, mute=None):
    enum = _enum()
    if not enum:
        return False
    dev = mgr = en = None
    done = False
    try:
        dev = ctypes.c_void_p()
        if _vt(enum, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p])(enum, 0, 1, ctypes.byref(dev)) != 0:
            return False
        mgr = ctypes.c_void_p()
        if _vt(dev, 3, ctypes.c_long, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p])(dev, _guid(IID_ASM2), 23, None, ctypes.byref(mgr)) != 0:
            return False
        en = ctypes.c_void_p()
        _vt(mgr, 5, ctypes.c_long, [ctypes.c_void_p])(mgr, ctypes.byref(en))
        cnt = ctypes.c_int()
        _vt(en, 3, ctypes.c_long, [ctypes.c_void_p])(en, ctypes.byref(cnt))
        for i in range(cnt.value):
            ctl = ctypes.c_void_p()
            if _vt(en, 4, ctypes.c_long, [ctypes.c_int, ctypes.c_void_p])(en, i, ctypes.byref(ctl)) != 0:
                continue
            c2 = ctypes.c_void_p()
            match = False
            if _vt(ctl, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(ctl, _guid(IID_ASC2), ctypes.byref(c2)) == 0:
                pv = ctypes.c_uint32()
                _vt(c2, 14, ctypes.c_long, [ctypes.c_void_p])(c2, ctypes.byref(pv))
                match = pv.value == pid
                _release(c2)
            if match:
                sv = ctypes.c_void_p()
                if _vt(ctl, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(ctl, _guid(IID_SAV), ctypes.byref(sv)) == 0:
                    if vol is not None:
                        _vt(sv, 3, ctypes.c_long, [ctypes.c_float, ctypes.c_void_p])(sv, max(0.0, min(1.0, vol / 100.0)), None)
                    if mute is not None:
                        _vt(sv, 5, ctypes.c_long, [ctypes.c_int, ctypes.c_void_p])(sv, 1 if mute else 0, None)
                    _release(sv)
                    done = True
            _release(ctl)
    except Exception as e:
        log(f"uygulama sesi ayarlama: {e}")
    finally:
        _release(en)
        _release(mgr)
        _release(dev)
        _release(enum)
    return done


# ---------- Makro / kısayol düğmeleri ----------
VK = {**{c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
      "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
      "space": 0x20, "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "printscreen": 0x2C,
      "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
      "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B, "plus": 0xBB, "minus": 0xBD,
      "+": 0xBB, "-": 0xBD, ".": 0xBE, ",": 0xBC}


def parse_combo(text):
    vks = []
    for part in str(text).replace(" ", "").split("+"):
        v = VK.get(part.lower())
        if v is None and len(part) == 1:
            v = ord(part.upper())
        if v is None:
            return None
        vks.append(v)
    return vks or None


def run_macro(idx):
    macros = _data().get("macros", [])
    if 0 <= idx < len(macros):
        vks = parse_combo(macros[idx]["keys"])
        if vks:
            press_combo(vks)
            return True
    return False


# ---------- Otomatik yedekleme ----------
import zipfile

BACKUP_DIR = os.path.join(_BASE, "Yedekler")


def backup_now(b):
    src = b.get("src", "")
    if not src or not os.path.isdir(src):
        return {"error": "folder"}
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = re.sub(r'[<>:"/\\|?*]', "_", str(b.get("name", "")).strip()[:40]) or "yedek"
    sub = os.path.join(BACKUP_DIR, name)
    os.makedirs(sub, exist_ok=True)
    zp = os.path.join(sub, time.strftime("%Y-%m-%d_%H-%M") + ".zip")
    total = 0
    try:
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
            for root, _, files in os.walk(src):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        if total + sz > 2 * 2**30:
                            raise IOError("yedek 2 GB sınırını aştı")
                        z.write(fp, os.path.relpath(fp, src))
                        total += sz
                    except OSError:
                        pass
    except Exception as e:
        log(f"yedek hatası ({name}): {e}")
        try:
            os.remove(zp)
        except OSError:
            pass
        return {"error": "zip"}
    keep = max(1, min(30, int(b.get("keep", 5))))
    zips = sorted(glob.glob(os.path.join(sub, "*.zip")), key=os.path.getmtime, reverse=True)
    for old in zips[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    log(f"yedek alındı: {name} ({round(total / 2**20)} MB)")
    return {"ok": True, "size": os.path.getsize(zp)}


def backup_list():
    out = []
    for b in _data().get("backups", []):
        sub = os.path.join(BACKUP_DIR, re.sub(r'[<>:"/\\|?*]', "_", b.get("name", "")))
        zips = sorted(glob.glob(os.path.join(sub, "*.zip")), key=os.path.getmtime, reverse=True) if os.path.isdir(sub) else []
        last = os.path.getmtime(zips[0]) if zips else 0
        out.append({**b, "count": len(zips), "last": int(last),
                    "size": sum(os.path.getsize(z) for z in zips), "exists": os.path.isdir(b.get("src", ""))})
    return out


def backup_loop():
    while True:
        time.sleep(600)
        try:
            now = time.time()
            for i, b in enumerate(_data().get("backups", [])):
                if not b.get("auto"):
                    continue
                sub = os.path.join(BACKUP_DIR, re.sub(r'[<>:"/\\|?*]', "_", b.get("name", "")))
                zips = sorted(glob.glob(os.path.join(sub, "*.zip")), key=os.path.getmtime, reverse=True) if os.path.isdir(sub) else []
                last = os.path.getmtime(zips[0]) if zips else 0
                if now - last >= b.get("every", 6) * 3600:
                    backup_now(b)
        except Exception as e:
            log(f"otomatik yedek: {e}")



# ---------- Sosyal: hediye, ziyaretçi, mini oyun ----------
def send_visit(code):
    cid = "d:" + code
    topic, key = conv_target(cid)
    if topic:
        env = chat_env("visit")
        ntfy_publish(topic, seal(key, env))


def apply_gift(env):
    g = env.get("gkind")
    with _lock:
        d = _data()
        if g == "coins":
            try:
                amt = max(1, min(10**6, int(env.get("gamt", 0))))
            except (TypeError, ValueError):
                return
            add_coins(amt, f"Hediye: {env.get('from', {}).get('n', 'arkadaş')}", d)
        elif g == "item" and env.get("gitem") in CAT:
            d["owned"] = sorted(set(d.get("owned", [])) | {env["gitem"]})
        _save(d)
    check_badges()


def gift_send(cid, kind, amt=0, item=""):
    if not cid.startswith("d:"):
        return "conv"
    with _lock:
        d = _data()
        if kind == "coins":
            try:
                amt = int(amt)
            except (TypeError, ValueError):
                return "bad"
            if amt < 1:
                return "bad"
            if d.get("coins", START_COINS) < amt:
                return "coins"
            add_coins(-amt, f"Hediye gönderildi", d)
            _save(d)
            txt = f"🎁 {amt} jeton hediye etti"
        elif kind == "item" and item in CAT and not CAT[item].get("custom"):
            txt = f"🎁 {CAT[item]['n']} hediye etti"
        else:
            return "bad"
    err = chat_send(cid, "gift", text=txt, gkind=kind, gamt=amt, gitem=item)
    if err and kind == "coins":  # geri al
        add_coins(amt, "Hediye iptal", None)
    return err


# ---------- Taş-kağıt-makas ----------
RPS = {"tas": "✊ Taş", "kagit": "✋ Kağıt", "makas": "✌️ Makas"}


def _rps_winner(a, b):
    if a == b:
        return 0
    beats = {"tas": "makas", "kagit": "tas", "makas": "kagit"}
    return 1 if beats[a] == b else 2


def rps_start(cid, bet):
    if not cid.startswith("d:"):
        return {"error": "conv"}
    try:
        bet = max(1, min(100000, int(bet)))
    except (TypeError, ValueError):
        return {"error": "bad"}
    if _data().get("coins", START_COINS) < bet:
        return {"error": "coins"}
    choice = random.choice(list(RPS))
    salt = secrets.token_hex(8)
    commit = hashlib.sha256(f"{choice}|{salt}".encode()).hexdigest()
    mid = secrets.token_hex(8)
    with _lock:
        d = _data()
        d.setdefault("rps", {})[mid] = {"choice": choice, "salt": salt, "bet": bet, "cid": cid}
        _save(d)
    err = ntfy_send_rps(cid, {"v": 1, "mid": mid, "t": "rps", "from": _rps_from(), "ts": int(time.time()),
                              "bet": bet, "commit": commit})
    return {"error": err} if err else {"ok": True}


def _rps_from():
    me = chat_me()
    return {"c": me.get("code", ""), "n": prof_get()[0]["name"]}


def ntfy_send_rps(cid, env):
    topic, key = conv_target(cid)
    if not topic:
        return "conv"
    err = ntfy_publish(topic, seal(key, env))
    if not err:
        _store_msg(cid, env, mine=True)
    return err


def rps_accept(cid, ref, choice):
    if choice not in RPS:
        return {"error": "bad"}
    msg = next((m for m in chat_load().get("msgs", {}).get(cid, []) if m.get("mid") == ref), None)
    if not msg or msg.get("t") != "rps":
        return {"error": "bad"}
    bet = int(msg.get("bet", 0))
    if _data().get("coins", START_COINS) < bet:
        return {"error": "coins"}
    topic, key = conv_target(cid)
    env = {"v": 1, "mid": secrets.token_hex(6), "t": "rpsmove", "from": _rps_from(), "ts": int(time.time()),
           "ref": ref, "phase": "accept", "choice": choice}
    err = ntfy_publish(topic, seal(key, env))
    if not err:
        _apply_rps_update(cid, ref, {"cB": choice, "bc": chat_me().get("code")}, local=True)
    return {"error": err} if err else {"ok": True}


def handle_rps_move(cid, env):
    ref, phase = env.get("ref"), env.get("phase")
    me = chat_me().get("code")
    frm = env.get("from", {}).get("c")
    if phase == "accept" and env.get("choice") in RPS:
        _apply_rps_update(cid, ref, {"cB": env["choice"], "bc": frm})
        # Ben meydan okuyansam, şimdi açıkla
        pend = _data().get("rps", {}).get(ref)
        if pend and frm != me:
            topic, key = conv_target(cid)
            rev = {"v": 1, "mid": secrets.token_hex(6), "t": "rpsmove", "from": _rps_from(), "ts": int(time.time()),
                   "ref": ref, "phase": "reveal", "choice": pend["choice"], "salt": pend["salt"]}
            ntfy_publish(topic, seal(key, rev))
            _resolve_rps(cid, ref, pend["choice"], env["choice"], pend["bet"], i_am_challenger=True)
    elif phase == "reveal" and env.get("choice") in RPS:
        if frm == me:
            return  # kendi açıklamamızı işleme (meydan okuyan zaten kabulde çözdü)
        msg = next((m for m in chat_load().get("msgs", {}).get(cid, []) if m.get("mid") == ref), None)
        if not msg or msg.get("done"):
            return
        commit = msg.get("commit", "")
        if hashlib.sha256(f"{env['choice']}|{env.get('salt','')}".encode()).hexdigest() != commit:
            log("rps: commit doğrulanamadı")
            return
        cB = msg.get("cB")
        if not cB:
            return
        _resolve_rps(cid, ref, env["choice"], cB, int(msg.get("bet", 0)), i_am_challenger=False)


def _apply_rps_update(cid, ref, fields, local=False):
    with _chat_lock:
        d = chat_load()
        for m in d.get("msgs", {}).get(cid, []):
            if m.get("mid") == ref:
                m.update(fields)
                m["u"] = int(time.time())
                chat_save(d)
                return


def _resolve_rps(cid, ref, cChal, cAcc, bet, i_am_challenger):
    msg = next((m for m in chat_load().get("msgs", {}).get(cid, []) if m.get("mid") == ref), None)
    if msg and msg.get("done"):
        return
    w = _rps_winner(cChal, cAcc)  # 1=challenger, 2=accepter, 0=tie
    i_won = (w == 1) if i_am_challenger else (w == 2)
    net = 0 if w == 0 else (bet if i_won else -bet)
    if net:
        add_coins(net, "Taş-kağıt-makas", None)
    _apply_rps_update(cid, ref, {"cChal": cChal, "cAcc": cAcc, "result": w, "done": 1})
    with _lock:
        d = _data()
        d.get("rps", {}).pop(ref, None)
        _save(d)
    name = {1: "kazandın" if i_won else "kaybettin", 2: "kazandın" if i_won else "kaybettin", 0: "berabere"}[w]
    notify("chat", "Taş-kağıt-makas", f"{RPS[cChal].split()[1]} vs {RPS[cAcc].split()[1]} — {name}" + (f" ({net:+} jeton)" if net else ""))


def visitors_list():
    vis = _data().get("visitors", {})
    out = [{"code": c, "name": v.get("n") or c, "ts": v.get("ts", 0), "count": v.get("count", 1)} for c, v in vis.items()]
    return sorted(out, key=lambda x: -x["ts"])[:20]


# ---------- Haftalık özet ----------
def weekly_summary():
    act = activity()
    d = _data()
    led = d.get("ledger", [])
    wk_start = time.time() - 7 * 86400
    earned = sum(l["amt"] for l in led if l["amt"] > 0 and l.get("ts", 0) >= wk_start)
    top = act["top"][0] if act["top"] else None
    days_played = sum(1 for x in act["days"] if x["min"] > 0)
    return {"total": act["total"], "top": top, "earned": earned, "days": days_played, "streak": act["streak"],
            "level": 1 + total_minutes() // 300, "name": prof_get()[0]["name"]}


# ---------- Bilgi yarışması (solo, günlük) ----------
QUIZ = [
    ("Steam'i hangi şirket geliştirdi?", ["Valve", "Epic", "EA", "Ubisoft"], 0),
    ("CS2 kaç kişiyle oynanır (5v5)?", ["10", "12", "6", "8"], 0),
    ("Minecraft'ı hangi şirket yaptı?", ["Mojang", "Rockstar", "Riot", "Blizzard"], 0),
    ("'GG' ne demek?", ["Good Game", "Great Goal", "Go Go", "Get Gold"], 0),
    ("League of Legends'ı kim geliştirdi?", ["Riot Games", "Valve", "Blizzard", "Epic"], 0),
    ("GTA serisini hangi şirket yapar?", ["Rockstar", "Ubisoft", "EA", "Bethesda"], 0),
    ("Fortnite'ı hangi şirket geliştirdi?", ["Epic Games", "Valve", "Activision", "Tencent"], 0),
    ("Elden Ring'in yapımcısı kim?", ["FromSoftware", "Capcom", "Square Enix", "Konami"], 0),
    ("Bir oyunda 'FPS' neyin kısaltması?", ["Frames Per Second", "First Player Shooter", "Fast Play System", "Free Play Store"], 0),
    ("The Witcher 3'ü hangi stüdyo yaptı?", ["CD Projekt Red", "Bethesda", "BioWare", "Ubisoft"], 0),
    ("Valorant'ı kim geliştirdi?", ["Riot Games", "Valve", "Blizzard", "Respawn"], 0),
    ("Overwatch hangi şirkete ait?", ["Blizzard", "Riot", "Valve", "EA"], 0),
    ("Red Dead Redemption 2'yi kim yaptı?", ["Rockstar", "Ubisoft", "Naughty Dog", "Capcom"], 0),
    ("Dota 2 hangi platformda yayınlanır?", ["Steam", "Epic", "Origin", "GOG"], 0),
    ("'NPC' ne demek?", ["Non-Player Character", "New Player Class", "Next Patch Content", "No Problem Can"], 0),
]


def quiz_today():
    tk = today_key()
    d = _data()
    q = d.get("quiz", {})
    if q.get("day") != tk:
        rnd = random.Random("q" + tk + (chat_me().get("code") or ""))
        idx = rnd.sample(range(len(QUIZ)), 5)
        order = []
        for i in idx:
            opts = QUIZ[i][1][:]
            correct = opts[QUIZ[i][2]]
            rnd.shuffle(opts)
            order.append({"i": i, "q": QUIZ[i][0], "opts": opts, "ans": opts.index(correct)})
        q = {"day": tk, "qs": order, "done": False}
        with _lock:
            dd = _data()
            dd["quiz"] = q
            _save(dd)
    return {"done": q.get("done", False), "qs": [{"q": x["q"], "opts": x["opts"]} for x in q["qs"]], "reward": 15}


def quiz_submit(answers):
    d = _data()
    q = d.get("quiz", {})
    if q.get("day") != today_key() or q.get("done"):
        return {"error": "done"}
    correct = sum(1 for i, x in enumerate(q["qs"]) if i < len(answers) and answers[i] == x["ans"])
    reward = correct * 15
    with _lock:
        dd = _data()
        dd["quiz"]["done"] = True
        add_coins(reward, f"Bilgi yarışması ({correct}/5)", dd)
        _save(dd)
    return {"ok": True, "correct": correct, "total": 5, "reward": reward,
            "ans": [x["ans"] for x in q["qs"]], "coins": _data().get("coins")}



# ---------- Oyun önerisi ve birlikte oynama ----------
def recommendations():
    installed = {g["appid"] for g in read_games()}
    d = chat_load()
    agg = {}
    for c, f in d.get("friends", {}).items():
        if f.get("state") != "ok":
            continue
        pr = f.get("profile") or {}
        games = []
        if pr.get("fav"):
            games.append(pr["fav"])
        games += pr.get("recent", [])
        if f.get("game"):
            games.append(f["game"])
        seen = set()
        for g in games:
            aid = str((g or {}).get("appid", ""))
            if not aid or aid in installed or aid in seen:
                continue
            seen.add(aid)
            e = agg.setdefault(aid, {"appid": aid, "name": g.get("name", ""), "who": [], "n": 0})
            e["n"] += 1
            e["who"].append(f.get("name") or c)
    out = sorted(agg.values(), key=lambda x: -x["n"])
    return out[:10]


def coplay_tick():
    try:
        app = running_appid()
        if app == "0":
            return
        now = time.time()
        with _chat_lock:
            d = chat_load()
            cp = d.setdefault("coplay", {})
            changed = False
            for c, f in d.get("friends", {}).items():
                if f.get("state") != "ok":
                    continue
                g = f.get("game")
                if g and f.get("seen", 0) > now - 2100:
                    e = cp.setdefault(c, {"total": 0, "same": 0, "n": ""})
                    e["total"] += 60
                    e["n"] = f.get("name") or c
                    if str(g.get("appid")) == app:
                        e["same"] += 60
                    e["last"] = int(now)
                    changed = True
            if changed:
                chat_save(d)
    except Exception as e:
        log(f"birlikte oynama: {e}")


def coplay_list():
    d = chat_load()
    cur = {c: f for c, f in d.get("friends", {}).items() if f.get("state") == "ok"}
    out = []
    for c, e in d.get("coplay", {}).items():
        f = cur.get(c)
        name = (f.get("name") if f else None) or e.get("n") or c
        out.append({"code": c, "name": name, "total": round(e.get("total", 0) / 60), "same": round(e.get("same", 0) / 60),
                    "last": e.get("last", 0)})
    return sorted([o for o in out if o["total"]], key=lambda x: -x["total"])[:10]


# ---------- Arayüz ----------
PAGE = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Oyunlarım">
<meta name="theme-color" content="#1b2838">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>Oyunlarım</title>
<style>
:root{--bg:#1b2838;--panel:#223247;--line:#2f4560;--text:#e6edf5;--muted:#8fa3b8;--accent:#66c0f4;--ok:#7bd88f;--bad:#ff8a80;--warn:#ffc46b}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"SF Pro Text",system-ui,sans-serif}
body{padding:env(safe-area-inset-top) 16px calc(env(safe-area-inset-bottom) + 84px)}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:14px 0 8px}
h1{font-size:26px;font-weight:700;margin:0 0 10px;letter-spacing:-.02em}
h2{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600}
input[type=search],input[type=password]{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:17px;outline:none}
input:focus-visible{border-color:var(--accent)}
button{font-family:inherit;color:inherit}
.row{display:flex;gap:8px;margin:10px 0 2px;align-items:center}
.chip{border:1px solid var(--line);background:transparent;border-radius:999px;padding:7px 14px;font-size:14px;font-weight:600;color:var(--muted)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#0b1a28}
.row .sp{flex:1}
#now{display:none;align-items:center;gap:12px;margin:0 0 10px;padding:10px 10px 10px 14px;border-radius:14px;background:var(--panel);border:1px solid var(--ok)}
#now.on{display:flex}
#now div{flex:1;min-width:0}
#now small{display:block;color:var(--ok);font-size:12px;font-weight:600}
#now b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.danger{border:0;border-radius:10px;padding:10px 16px;background:var(--bad);color:#2a0b08;font-weight:700;font-size:15px}
.hero{position:relative;display:block;width:100%;border:0;padding:0;border-radius:var(--r,18px);overflow:hidden;margin:10px 0 4px;background:var(--panel);text-align:left}
.hero img{display:block;width:100%;aspect-ratio:460/215;object-fit:cover}
.hero .cap{position:absolute;inset:auto 0 0 0;padding:34px 14px 12px;background:linear-gradient(transparent,rgba(10,18,28,.92))}
.hero .cap{color:#e6edf5}.hero b{display:block;font-size:19px}.hero small{color:#9fb2c6}
.hero .go{position:absolute;right:12px;bottom:12px;background:var(--ok);color:#0a2412;font-weight:700;border-radius:10px;padding:9px 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--cw,150px),1fr));gap:12px;margin-top:10px}
.game{position:relative;border:0;padding:0;border-radius:var(--r,14px);overflow:hidden;background:var(--panel);text-align:left;cursor:pointer;display:block;width:100%}
.game img{display:block;width:100%;aspect-ratio:460/215;object-fit:cover;background:#16202d}
.game span{display:block;padding:9px 10px 0;font-size:14px;font-weight:600;line-height:1.25}
.game em{display:block;padding:2px 10px 10px;font-size:12px;color:var(--muted);font-style:normal}
.game:active,.hero:active{transform:scale(.97);transition:transform .1s}
.game.busy{outline:2px solid var(--accent)}.game.live{outline:2px solid var(--ok)}
.star{position:absolute;top:6px;right:6px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(10,18,28,.65);font-size:17px;line-height:32px;padding:0;color:#fff}
.star.on{color:var(--warn)}
.card{background:var(--panel);border-radius:var(--r,16px);padding:14px;margin-bottom:12px}
.bar{margin:0 0 12px}.bar:last-child{margin:0}
.bar div{display:flex;justify-content:space-between;font-size:14px;margin-bottom:5px}.bar i{font-style:normal;color:var(--muted)}
.bar p{height:8px;border-radius:4px;background:var(--bg);margin:0;overflow:hidden}.bar p b{display:block;height:100%;background:var(--accent);transition:width .6s}
.btns{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.btn{border:1px solid var(--line);background:transparent;border-radius:12px;padding:13px 8px;font-size:15px;font-weight:600}
.btn:active{background:var(--line)}.btn.bad{color:var(--bad)}
#shot{width:100%;border-radius:10px;margin-top:10px}
.me{display:flex;gap:14px;align-items:center}
.av{width:72px;height:72px;border-radius:12px;background:var(--line);object-fit:cover;display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:700;flex:none}
.me>div{flex:1;min-width:0}.me h1{margin:0 0 4px;font-size:22px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.me small{color:var(--muted);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.on{background:var(--accent)}.dot.play{background:var(--ok)}
.lvl{min-width:42px;height:42px;border-radius:50%;border:3px solid var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700}
.sts{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.st{background:var(--bg);border-radius:12px;padding:12px}.st b{display:block;font-size:20px}.st span{font-size:12px;color:var(--muted)}
.top{display:flex;gap:10px;align-items:center;margin-bottom:10px}.top:last-child{margin:0}
.top img{width:84px;border-radius:6px;aspect-ratio:460/215;object-fit:cover;background:var(--bg)}
.top>div{flex:1;min-width:0}.tn{display:flex;justify-content:space-between;gap:8px;font-size:14px;font-weight:600;margin-bottom:5px}
.tn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tn i{font-style:normal;color:var(--muted);white-space:nowrap}
.top p{height:6px;border-radius:3px;background:var(--bg);margin:0;overflow:hidden}.top p b{display:block;height:100%;background:var(--accent)}
.note{color:var(--muted);font-size:14px;line-height:1.5;margin:0}
[hidden]{display:none!important}
.card select,.card input[readonly]{width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px}
[hidden]{display:none!important}
#shot.full{position:fixed;inset:0;z-index:20;width:100%;height:100%;object-fit:contain;background:#000;margin:0;border-radius:0}
#fsx{display:none;position:fixed;top:calc(env(safe-area-inset-top) + 10px);right:12px;z-index:21}
#shot.full+#fsx{display:block}
.chart{display:flex;align-items:flex-end;gap:6px;height:120px}
.chart div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;font-size:11px;color:var(--muted);gap:3px}
.chart i{display:block;width:100%;background:var(--accent);border-radius:6px 6px 0 0;min-height:2px}
.card input[type=text],.card input[type=time]{width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px;outline:none}
.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:4px;scrollbar-width:none}.strip::-webkit-scrollbar{display:none}
.mini{flex:none;width:140px;border:0;padding:0;border-radius:12px;overflow:hidden;background:var(--panel);text-align:left}
.mini img{display:block;width:100%;aspect-ratio:460/215;object-fit:cover}
.mini span{display:block;padding:6px 8px 8px;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.game,.mini{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
#sheet{position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.55);display:flex;align-items:flex-end}
.sh{width:100%;background:var(--panel);border-radius:20px 20px 0 0;padding:18px 16px calc(env(safe-area-inset-bottom) + 16px)}
.sh h2{margin:0 0 12px;color:var(--text);font-size:17px}
.sh input{width:100%;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:16px;margin-bottom:10px;outline:none}
.sh .btns{grid-template-columns:1fr}
body.lst .grid{grid-template-columns:1fr}
body.lst .game{display:grid;grid-template-columns:40% 1fr;align-items:center}
body.lst .game img{grid-row:1/3}body.lst .game span{padding:0 12px}body.lst .game em{padding:2px 12px 0}
body.nocov .game img,body.nocov .mini img{display:none}
img.noimg{background:linear-gradient(135deg,#2f4560,#223247)}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;background:var(--panel);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom);z-index:8}
nav button{flex:1;border:0;background:none;padding:15px 0;font-size:12.5px;font-weight:600;color:var(--muted)}
nav button.on{color:var(--accent)}
#toast{position:fixed;left:16px;right:16px;bottom:calc(env(safe-area-inset-bottom) + 72px);padding:14px 16px;border-radius:14px;background:var(--panel);border:1px solid var(--line);font-weight:600;opacity:0;transform:translateY(12px);transition:.2s;pointer-events:none;z-index:9}
#toast.show{opacity:1;transform:none}#toast.ok{border-color:var(--ok)}#toast.err{border-color:var(--bad)}
.center{max-width:360px;margin:20vh auto 0}.center p{color:var(--muted);margin:0 0 14px;line-height:1.4}
button.primary{margin-top:10px;width:100%;padding:13px;border:0;border-radius:12px;background:var(--accent);color:#0b1a28;font-size:17px;font-weight:700}
.empty{color:var(--muted);padding:40px 0;text-align:center}
.game small.tg{display:block;padding:0 10px 9px;margin-top:-6px;font-size:11px;font-weight:600;color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
body.lst .game img{grid-row:1/4}body.lst .game small.tg{padding:2px 12px 0;margin:0}
#tagrow{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin-top:8px}#tagrow::-webkit-scrollbar{display:none}#tagrow .chip{flex:none}#tagrow:empty{display:none}
.fr{display:flex;align-items:center;gap:10px;margin-bottom:10px}.fr:last-child{margin-bottom:0}.fr>div{flex:1;min-width:0}
.fr b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.fr span{display:block;font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr span.g{color:var(--ok)}.fr span.o{color:var(--accent)}.fr .chip{flex:none;padding:6px 11px;font-size:13px}
.fa{width:40px;height:40px;border-radius:8px;background:var(--bg);flex:none;object-fit:cover}
.fh{font-size:13px;color:var(--muted);font-weight:600;margin:14px 0 8px}.fh:first-of-type{margin-top:0}
.pf{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.pt{background:var(--bg);border-radius:12px;padding:10px 12px;min-width:0}.pt span{display:block;font-size:12px;color:var(--muted)}.pt b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.pt svg{display:block;width:100%;height:28px;margin-top:6px}
.card input[type=password]{width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px;outline:none}
.sh{max-height:88vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
.ach{display:flex;gap:10px;align-items:center;margin-bottom:10px}.ach img{width:44px;height:44px;border-radius:8px;background:var(--bg);flex:none}
.ach>div{flex:1;min-width:0}.ach b{display:block;font-size:14px}.ach span{display:block;font-size:12px;color:var(--muted);line-height:1.35}
.ach i{font-style:normal;font-size:12px;color:var(--muted);white-space:nowrap}.ach.lk img{opacity:.5;filter:grayscale(1)}
.tp{height:230px;border-radius:14px;background:var(--bg);border:1px dashed var(--line);touch-action:none;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:13px;line-height:1.6;padding:20px;-webkit-user-select:none;user-select:none}
.tp.act{border-color:var(--accent)}
.fmini{flex:none;width:74px;text-align:center;border:0;background:none;padding:0}.fmini .fa{width:52px;height:52px;border-radius:12px;margin:0 auto 6px;display:flex;align-items:center;justify-content:center;font-weight:700}
.fmini b{display:block;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fmini span{display:block;font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fmini span.g{color:var(--ok)}.fmini span.o{color:var(--accent)}
.hrs{display:flex;align-items:flex-end;gap:2px;height:64px}.hrs i{flex:1;background:var(--accent);border-radius:2px 2px 0 0;min-height:2px}
.hrl{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}
.chip:disabled{opacity:.35}
.pr{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}.pr:last-child{border:0}
.pr>div{flex:1;min-width:0}.pr b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pr span{display:block;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pr i{font-style:normal;font-size:13px;font-variant-numeric:tabular-nums;text-align:right;min-width:62px;color:var(--muted)}.pr i.hot{color:var(--warn)}
input[type=range]{width:100%;accent-color:var(--accent);height:28px;margin:4px 0 10px}
.wl{display:flex;gap:10px;align-items:center;margin-bottom:12px}.wl img{width:110px;border-radius:8px;aspect-ratio:460/215;object-fit:cover;background:var(--bg);flex:none}
.wl>div{flex:1;min-width:0}.wl b{display:block;font-size:14px;line-height:1.3}.wl s{color:var(--muted);font-size:12px;margin-right:6px}
.disc{display:inline-block;background:#4c6b22;color:#beee11;font-weight:700;font-size:12px;padding:2px 6px;border-radius:4px;margin-right:6px}
.wl .pp{font-size:13px;margin-top:4px}
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.sg button{border:0;padding:0;border-radius:10px;overflow:hidden;background:var(--bg);aspect-ratio:16/9;position:relative}
.sg img{width:100%;height:100%;object-fit:cover;display:block}.sg span{position:absolute;left:0;right:0;bottom:0;padding:14px 6px 4px;font-size:11px;text-align:left;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.75));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#viewer{position:fixed;inset:0;z-index:40;background:#000;display:flex;flex-direction:column}
#viewer img{flex:1;min-height:0;width:100%;object-fit:contain}
#viewer .vb{display:flex;gap:8px;padding:12px 16px calc(env(safe-area-inset-bottom) + 12px);align-items:center;color:#ccc;font-size:13px}
#viewer .vb a,#viewer .vb button{flex:none}
.ptt{width:100%;padding:26px 0;border-radius:16px;border:2px solid var(--accent);background:transparent;color:var(--accent);font-size:17px;font-weight:700;-webkit-user-select:none;user-select:none;touch-action:none}
.pb{position:relative;border-radius:var(--r,18px);overflow:hidden;margin-bottom:12px;padding:26px 16px 16px;text-align:center;color:#fff;background-size:cover}
.pb::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,0) 35%,rgba(0,0,0,.55));pointer-events:none}.pb>*{position:relative;z-index:1}
.pb h1{margin:10px 0 2px;font-size:24px;text-shadow:0 1px 6px rgba(0,0,0,.6)}.pb p{margin:0 auto;max-width:300px;font-size:14px;opacity:.92;text-shadow:0 1px 4px rgba(0,0,0,.6);white-space:pre-wrap}
.pb .lv{display:inline-block;margin-top:8px;padding:3px 10px;border-radius:999px;background:rgba(0,0,0,.45);font-size:12px;font-weight:700}
.pfr{display:inline-flex;align-items:center;justify-content:center;border-radius:50%;padding:4px;flex:none}
.pav{border-radius:50%;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#2f4560;border:2px solid rgba(0,0,0,.35)}.pav img{width:100%;height:100%;object-fit:cover}
.an-spin{background-size:200% 200%;animation:hue 4s linear infinite}.an-pulse{animation:pulse 1.8s ease-in-out infinite}.an-flow{background-size:300% 300%!important;animation:flow 10s ease infinite}
@keyframes hue{to{filter:hue-rotate(360deg)}}@keyframes pulse{50%{filter:brightness(1.5)}}@keyframes flow{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
.coin{display:inline-flex;align-items:center;gap:5px;font-weight:700;color:#ffd54f}
.shg{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.shi{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--bg);text-align:left;padding:0;display:flex;flex-direction:column}
.shi .pv{height:84px;display:flex;align-items:center;justify-content:center;background-size:cover}.shi div.ib{padding:8px 10px 10px}
.shi b{display:block;font-size:14px}.shi span{display:block;font-size:12px;color:var(--muted);margin:2px 0 8px}.shi .chip{width:100%}
.shi.on{border-color:var(--accent)}
.tsw{display:flex;gap:4px}.tsw i{width:18px;height:40px;border-radius:5px;border:1px solid rgba(255,255,255,.15)}
.qst{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.qst:last-child{border:0}.qst>div{flex:1;min-width:0}
.qst b{display:block;font-size:14px}.qst p{height:6px;border-radius:3px;background:var(--bg);margin:6px 0 0;overflow:hidden}.qst p i{display:block;height:100%;background:var(--ok)}
.qst span{font-size:12px;color:var(--muted)}
.bdg{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px}
.bdg div{background:var(--bg);border-radius:12px;padding:10px 6px;text-align:center;font-size:12px;color:var(--muted)}.bdg div b{display:block;font-size:26px;filter:grayscale(1);opacity:.35}
.bdg div.on{color:var(--text)}.bdg div.on b{filter:none;opacity:1}
.whl{position:relative;width:280px;height:280px;margin:6px auto 14px}.whl svg{width:100%;height:100%;transition:transform 4.2s cubic-bezier(.17,.67,.12,1)}
.whl:after{content:"";position:absolute;top:-6px;left:50%;margin-left:-12px;border:12px solid transparent;border-top:22px solid #ffd54f;filter:drop-shadow(0 2px 3px rgba(0,0,0,.5))}
.lb{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.lb:last-child{border:0}.lb>i{font-style:normal;width:26px;text-align:center;font-weight:700;color:var(--muted)}
.lb>div:not(.pfr):not(.cav){flex:1;min-width:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lb>b{font-variant-numeric:tabular-nums}.lb.me{background:rgba(102,192,244,.12);border-radius:10px;padding:8px 6px}
.ann{background:linear-gradient(135deg,rgba(255,196,107,.18),rgba(102,192,244,.12));border:1px solid var(--warn);border-radius:var(--r,16px);padding:12px 14px;margin-bottom:12px}
.ann b{display:block;margin-bottom:4px}.ann p{margin:0;font-size:14px;color:var(--text);white-space:pre-wrap}
.evt,.pol{align-self:flex-start;width:86%;background:var(--panel);border:1px solid var(--accent);border-radius:16px;overflow:hidden}.evt.mine,.pol.mine{align-self:flex-end}
.evt img{display:block;width:100%;aspect-ratio:460/215;object-fit:cover;background:var(--bg)}.evt>div,.pol>div{padding:10px 12px}
.evt b,.pol b{display:block;font-size:15px}.evt p,.pol p{margin:2px 0 8px;font-size:13px;color:var(--muted)}
.opt{display:block;width:100%;text-align:left;position:relative;border:1px solid var(--line);background:var(--bg);border-radius:10px;padding:9px 10px;margin-bottom:6px;font-size:14px;overflow:hidden}
.opt i{position:absolute;inset:0 auto 0 0;background:rgba(102,192,244,.25);z-index:0}.opt span{position:relative;z-index:1}.opt em{position:relative;z-index:1;float:right;font-style:normal;color:var(--muted)}.opt.on{border-color:var(--accent)}
.rx{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}.rx span{background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:1px 7px;font-size:13px;color:var(--text)}.rx span.on{border-color:var(--accent)}
.bub.mine .rx span{background:rgba(255,255,255,.35);border-color:transparent;color:#0b1a28}
.bub audio{display:block;width:230px;max-width:100%;height:40px}
.rpk{display:flex;justify-content:space-around;font-size:32px}.rpk button{border:0;background:none;padding:6px}
.cmp button.rec{background:var(--bad);color:#2a0b08}
.np{display:flex;align-items:center;gap:12px;background:var(--bg);border-radius:12px;padding:10px 12px;margin-bottom:10px}
.np .ic{width:44px;height:44px;border-radius:10px;background:var(--panel);display:flex;align-items:center;justify-content:center;font-size:24px;flex:none}
.np>div{flex:1;min-width:0}.np b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.np span{display:block;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.av2{display:flex;align-items:center;gap:10px;margin-bottom:10px}.av2>span{flex:none;width:92px;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.av2 input{flex:1}.av2 button{flex:none;width:34px;height:30px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--muted);font-size:14px}.av2 button.on{color:var(--bad);border-color:var(--bad)}
.ae{display:inline-block}.ae.a-bounce{animation:bnc .9s ease-in-out infinite}.ae.a-spin{animation:hue2 3s linear infinite,spn 3s linear infinite}
.ae.a-beat{animation:beat 1s ease-in-out infinite}.ae.a-flick{animation:flick .25s ease-in-out infinite}.ae.a-disco{animation:bnc .5s ease infinite,hue2 1.5s linear infinite}
@keyframes bnc{0%,100%{transform:translateY(8%)}50%{transform:translateY(-14%)}}@keyframes spn{to{transform:rotate(360deg)}}@keyframes beat{0%,100%{transform:scale(1)}15%{transform:scale(1.25)}30%{transform:scale(1)}45%{transform:scale(1.18)}}@keyframes flick{0%,100%{transform:scale(1) rotate(-3deg)}50%{transform:scale(1.12) rotate(3deg)}}@keyframes hue2{to{filter:hue-rotate(360deg)}}
.show{display:flex;gap:6px;justify-content:center;margin-top:8px}.show span{font-size:22px;filter:drop-shadow(0 1px 3px rgba(0,0,0,.6))}
.wgal{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:4px}
.wgal button{border:1px solid var(--line);border-radius:12px;background:var(--bg);padding:12px 8px;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:13px;color:var(--text)}
.wgal button b{font-size:26px}
.gift{align-self:center;background:linear-gradient(135deg,rgba(255,196,107,.25),rgba(255,138,128,.15));border:1px solid var(--warn);border-radius:16px;padding:12px 16px;text-align:center;max-width:82%}.gift b{display:block;font-size:26px}.gift span{font-size:14px}
.rps{align-self:flex-start;width:84%;background:var(--panel);border:1px solid var(--accent);border-radius:16px;padding:12px;text-align:center}.rps.mine{align-self:flex-end}
.rps b{display:block;font-size:15px;margin-bottom:4px}.rps .pick{display:flex;gap:8px;justify-content:center;margin:8px 0}.rps .pick button{flex:1;border:1px solid var(--line);background:var(--bg);border-radius:12px;padding:12px 4px;font-size:22px}
.rps .res{font-size:17px;font-weight:700;margin-top:6px}
.vis{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}.vis:last-child{border:0}.vis>div:not(.cav){flex:1;min-width:0}.vis b{display:block;font-size:14px}.vis span{font-size:12px;color:var(--muted)}.vis i{font-style:normal;font-size:12px;color:var(--muted)}
.wk{background:linear-gradient(135deg,#1a2980,#26d0ce);border-radius:16px;padding:16px;color:#fff;margin-bottom:12px}.wk h3{margin:0 0 10px;font-size:17px}.wk .sts .st{background:rgba(0,0,0,.25)}.wk .st b,.wk .st span{color:#fff}
nav button{position:relative}nav b.nb{position:absolute;top:7px;left:calc(50% + 14px);min-width:17px;height:17px;padding:0 4px;border-radius:9px;background:var(--bad);color:#2a0b08;font-size:11px;line-height:17px}
.cv{display:flex;gap:12px;align-items:center;width:100%;border:0;background:none;padding:10px 0;text-align:left;border-bottom:1px solid var(--line)}.cv:last-child{border:0}
.cav{width:44px;height:44px;border-radius:50%;background:var(--line);flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:17px;color:var(--text)}.cav.room{border-radius:12px}
.cv>div:not(.cav):not(.pfr){flex:1;min-width:0}.cv b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cv span{display:block;font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cv span.g{color:var(--ok)}
.cv i{font-style:normal;font-size:12px;color:var(--muted);text-align:right;flex:none}.cv em{display:block;font-style:normal;margin-top:4px;margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent);color:#0b1a28;font-size:12px;font-weight:700;line-height:20px;text-align:center;width:max-content}
.code{font-size:22px;font-weight:700;letter-spacing:.06em;font-variant-numeric:tabular-nums;margin:2px 0 10px}
#msgs{display:flex;flex-direction:column;gap:6px;padding:4px 0 12px}
.bub{max-width:80%;padding:8px 12px;border-radius:16px;background:var(--panel);align-self:flex-start;font-size:15px;line-height:1.4;word-wrap:break-word;white-space:pre-wrap}
.bub.mine{align-self:flex-end;background:var(--accent);color:#0b1a28}.bub small{display:block;font-size:12px;font-weight:700;color:var(--accent);margin-bottom:2px}
.bub time{display:block;font-size:11px;opacity:.6;margin-top:3px;text-align:right}.bub.ph{padding:4px;width:min(78%,300px)}.bub.ph img{display:block;width:100%;height:auto;max-height:360px;object-fit:cover;border-radius:12px}.bub.ph small,.bub.ph time{padding:0 8px}
.sys{align-self:center;font-size:12px;color:var(--muted);padding:4px 0}
.inv{align-self:flex-start;width:84%;background:var(--panel);border:1px solid var(--ok);border-radius:16px;overflow:hidden}.inv.mine{align-self:flex-end;border-color:var(--accent)}
.inv img{display:block;width:100%;aspect-ratio:460/215;object-fit:cover;background:var(--bg)}.inv div{padding:10px 12px}.inv b{display:block;font-size:15px;margin-bottom:2px}.inv p{margin:0 0 8px;font-size:13px;color:var(--muted)}
.cmp{position:fixed;left:0;right:0;bottom:calc(env(safe-area-inset-bottom) + 47px);background:var(--bg);border-top:1px solid var(--line);padding:8px 12px;display:flex;gap:8px;align-items:center;z-index:7}
.cmp input[type=text]{flex:1;min-width:0;padding:11px 14px;border-radius:20px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:16px;outline:none}
.cmp button{flex:none;border:0;border-radius:50%;width:42px;height:42px;background:var(--panel);font-size:18px;color:var(--text)}.cmp button.snd{background:var(--accent);color:#0b1a28;font-weight:700}
body.inchat{padding-bottom:calc(env(safe-area-inset-bottom) + 150px)}
.ptt.rec{background:var(--bad);border-color:var(--bad);color:#2a0b08}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div id="app"></div>
<div id="toast" role="status"></div>
<script>
const $=s=>document.querySelector(s);
let LANG=(()=>{try{return localStorage.getItem("lang")||"tr"}catch(e){return "tr"}})();
const TX={"Kaldığın yerden devam et": "Resume where you left off", "İşlemci": "CPU", "Bellek": "RAM", "1 sa": "1 h", "2 sa": "2 h", "30 dk": "30 min", "Son oturum": "Last session", "Özet": "Overview", "Oyunlar": "Games", "Sohbet": "Chat", "PC": "PC", "Dosyalar": "Files", "Ayarlar": "Settings", "Oyunlarım": "My Games", "Admin": "Admin", "Mağaza": "Shop", "🛒 Mağaza": "🛒 Shop", "Profil": "Profile", "Favoriler": "Favorites", "Steam": "Steam", "Steam özellikleri": "Steam features", "Steam profilim": "My Steam profile", "Bilgisayar": "Computer", "Ara": "Search", "Oyun ara": "Search games", "Ad ya da kod ara": "Search name or code", "Aç": "Open", "Kıs": "Lower", "Ses": "Sound", "Ses & medya": "Sound & media", "Güç": "Power", "Kur": "Install", "Kur / Güncelle": "Install / Update", "Ad": "Name", "Oyna": "Play", "Ekle": "Add", "+ Ekle": "+ Add", "+ Klasör": "+ Folder", "Kaldır": "Remove", "Kapat": "Close", "Gönder": "Send", "Paylaş": "Share", "Kullan": "Use", "Yenile": "Refresh", "Katıl": "Join", "Kabul": "Accept", "Reddet": "Decline", "İndir": "Download", "İptal": "Cancel", "Sil": "Delete", "Durum": "Status", "Tümü": "All", "Özel": "Custom", "Düşük": "Low", "Orta": "Medium", "Yüksek": "High", "Sistem": "System", "Ekran": "Screen", "Ekranı gör": "View screen", "Ekranı kapat": "Turn off screen", "Ekranda göster": "Show on screen", "Mesaj": "Message", "Mesaj yaz": "Write a message", "Mesaj gönder": "Send message", "Başla": "Start", "Devam": "Continue", "Dokun": "Tap", "Çıkış": "Exit", "Çıkış yap": "Log out", "Mikrofon": "Microphone", "Touchpad": "Touchpad", "Monitör": "Monitor", "Avatar": "Avatar", "Çerçeve": "Frame", "Arka plan": "Background", "Etiketler": "Tags", "Yeni etiket": "New tag", "Bahis": "Bet", "Bahis gir.": "Enter a bet.", "Oyun süresi": "Playtime", "Bugün": "Today", "Satın al": "Buy", "Kopyala": "Copy", "Planla": "Schedule", "Planla ve paylaş": "Schedule & share", "Davet et": "Invite", "Meydan oku": "Challenge", "Sessiz": "Mute", "Sağırlaştır": "Deafen", "Oynat / Duraklat": "Play / Pause", "⏯ Oynat / Duraklat": "⏯ Play / Pause", "⏭ Sonraki": "⏭ Next", "⏮ Önceki": "⏮ Previous", "Ses +": "Vol +", "Ses −": "Vol −", "Ses çıkışı": "Audio output", "Hakkında": "About", "Başlık": "Title", "Jeton": "Coins", "Jeton geçmişi": "Coin history", "Güncelle": "Update", "🔄 Güncelleme": "🔄 Update", "Keşfet": "Discover", "Discord": "Discord", "Discord'u aç": "Open Discord", "Discord'a gönder": "Send to Discord", "Discord kanalına mesaj": "Message to Discord channel", "PIN": "PIN", "Yeni PIN": "New PIN", "PIN'i değiştir": "Change PIN", "Sunucu": "Server", "Sunucuyu kapat": "Stop server", "Programlar": "Programs", "Ödülü al": "Claim reward", "Ödül alınamadı.": "Couldn't claim reward.", "Enter ↵": "Enter ↵", "Sil ⌫": "Del ⌫", "Tam ekran": "Fullscreen", "Tek kare": "Single frame", "Sol tık": "Left click", "Sağ tık": "Right click", "Big Picture": "Big Picture", "İnterkom": "Intercom", "İstatistikler": "Statistics", "İstek listesi": "Wishlist", "Tüm istek listesi": "Full wishlist", "İndirmeler": "Downloads", "Canlı izle": "Watch live", "Canlı performans": "Live performance", "Dokunarak kontrol": "Touch control", "Başarımlar": "Achievements", "Başarımları gör": "View achievements", "Gizli oyunlar": "Hidden games", "Favori oyun": "Favorite game", "Favori oyun seç": "Pick a favorite game", "Favori oyunu:": "Favorite game:", "Görünen ad": "Display name", "Görünen adın": "Your display name", "Görünen adın:": "Your display name:", "Profili düzenle": "Edit profile", "✏️ Profili düzenle": "✏️ Edit profile", "Profili sıfırla": "Reset profile", "Profiline bak": "View profile", "Görünüm": "Appearance", "Görünümü sıfırla": "Reset appearance", "Özelleştirme": "Customization", "Arkadaşlar": "Friends", "Arkadaş ekle": "Add friend", "Arkadaş kodun": "Your friend code", "Arkadaşlıktan çıkar": "Unfriend", "Arkadaşlık istekleri": "Friend requests", "Oda kur": "Create room", "Odaya katıl": "Join room", "Oda kodu": "Room code", "Odadan çık": "Leave room", "Odanın adını değiştir": "Rename room", "Kodu kopyala": "Copy code", "Sohbete başla": "Start chat", "Sohbette paylaş": "Share in chat", "Günlük hedef": "Daily goal", "Mola ve günlük hedef": "Break & daily goal", "Oyun modu": "Game mode", "Oyun planlayıcı": "Game scheduler", "Oyun zamanlayıcısı": "Game timer", "Oyun klasörünü PC'de aç": "Open game folder on PC", "Oyunu kaldır": "Uninstall game", "Oyunu kapat": "Close game", "Oyun dosyalarını doğrula": "Verify game files", "Oyunları tara": "Scan games", "Oyunlarımdan doldur": "Fill from my games", "Rastgele": "Random", "Rastgele oyun": "Random game", "Ekran görüntüleri": "Screenshots", "Görev yöneticisi": "Task Manager", "Çalışan programları göster": "Show running programs", "Uygulama sesleri": "App volumes", "Pano köprüsü": "Clipboard bridge", "Makro / kısayol düğmeleri": "Macro / shortcut buttons", "Otomatik yedekleme": "Automatic backup", "Şimdi yedekle": "Back up now", "Yedek klasörünü PC'de aç": "Open backup folder on PC", "Henüz yedek yok.": "No backups yet.", "Son oturumlar": "Recent sessions", "Gece ışığı (mavi ışığı azaltır)": "Night light (reduces blue light)", "Windows gece ışığı ayarı": "Windows night light setting", "Parlaklık değiştirilemedi.": "Couldn't change brightness.", "Telefona bildirimler": "Phone notifications", "Test bildirimi": "Test notification", "Test mesajı": "Test message", "Kestirmeler (iPhone widget)": "Shortcuts (iPhone widget)", "Android uygulaması": "Android app", "Adres & QR kod": "Address & QR code", "QR göster": "Show QR", "Sunucu adresleri": "Server addresses", "Steam API anahtarı": "Steam API key", "Steam API anahtarı:": "Steam API key:", "SteamID:": "SteamID:", "Admin şifresi:": "Admin password:", "Admin oturumunu kapat": "Log out of admin", "Kullanıcı veritabanı": "User database", "Veritabanından sil": "Delete from database", "Herkese gönder": "Send to all", "📢 Duyuru gönder": "📢 Send announcement", "Arkadaşlara bu sürümü gönder": "Send this version to friends", "Mağaza görsellerini tekrar gönder": "Re-send store images", "Mağaza sayfası": "Store page", "Mağaza sayfası PC": "Store page on PC", "Klasörü PC'de aç": "Open folder on PC", "PC'den al": "Get from PC", "PC'ye gönder": "Send to PC", "PC'den telefona": "PC to phone", "Telefondan PC'ye": "Phone to PC", "Dosya seç ve gönder": "Choose a file and send", "Klasör boş.": "Folder is empty.", "Kimse yok.": "Nobody here.", "Oyun yok.": "No games.", "Eşleşen oyun yok.": "No matching games.", "Bulunamadı.": "Not found.", "Alınamadı.": "Couldn't load.", "Gönderilemedi.": "Couldn't send.", "Gönderiliyor…": "Sending…", "Gönderim sürüyor, bitmesini bekle.": "Transfer in progress, please wait.", "Kaydedildi.": "Saved.", "Kaydedilemedi.": "Couldn't save.", "Kaydet": "Save", "Değiştirilemedi.": "Couldn't change.", "Silindi.": "Deleted.", "Silinemedi.": "Couldn't delete.", "Kapatıldı.": "Turned off.", "Kapatılıyor…": "Turning off…", "Başlatılamadı.": "Couldn't start.", "Planlandı.": "Scheduled.", "Olmadı.": "Failed.", "Yazıldı.": "Done.", "Kopyalandı.": "Copied.", "Bağlantı kopyalandı.": "Link copied.", "Bağlantıyı kopyala": "Copy link", "Bağlantıyı basılı tutup kopyala.": "Press and hold the link to copy.", "Yükleniyor…": "Loading…", "Rapor hazırlanıyor…": "Preparing report…", "Resim hazırlanıyor…": "Preparing image…", "Resim hazırlanamadı.": "Couldn't prepare image.", "Resim gönderildi.": "Image sent.", "Resim açılamadı.": "Couldn't open image.", "Fotoğraf açılamadı.": "Couldn't open photo.", "Liste alınamadı.": "Couldn't load list.", "Dosya listesi alınamadı.": "Couldn't load file list.", "Dosya indirilemedi.": "Couldn't download file.", "Bilgi alınamadı.": "Couldn't load info.", "Profil alınamadı.": "Couldn't load profile.", "Profil bilgisi alınamadı.": "Couldn't load profile info.", "Favori kaydedilemedi.": "Couldn't save favorite.", "Etiket kaydedilemedi.": "Couldn't save tags.", "Not kaydedilemedi.": "Couldn't save note.", "Notu kaydet": "Save note", "Mikrofon izni verilmedi.": "Microphone permission denied.", "PC mikrofonu açılamadı.": "Couldn't open PC microphone.", "PC sesi alınamadı.": "Couldn't get PC audio.", "Ekran alınamadı, canlı yayın durdu.": "Couldn't capture screen, live view stopped.", "Ekran görüntüsü alınamadı.": "Couldn't take screenshot.", "PC panosuna kopyalandı.": "Copied to PC clipboard.", "Mesaj PC ekranında gösterildi.": "Message shown on PC screen.", "Komut gönderildi.": "Command sent.", "PC'ye ulaşılamadı.": "Couldn't reach PC.", "Tema uygulandı.": "Theme applied.", "Zamanlayıcı kapalı.": "Timer off.", "Zaten tamamladın.": "Already completed.", "Bugünkü yarışmayı tamamladın.": "You finished today's quiz.", "Hepsini açmışsın, tebrikler!": "You've unlocked them all, congrats!", "Hepsinin resmi var.": "All of them have images.", "Tüm oyunlar güncel.": "All games are up to date.", "Henüz açılan başarım yok.": "No achievements unlocked yet.", "Henüz hareket yok.": "No activity yet.", "Henüz mesaj yok. İlk mesajı sen yaz!": "No messages yet. Be the first to write!", "Henüz sohbetin yok. Aşağıdan arkadaş ekle ya da oda kur.": "No chats yet. Add a friend or create a room below.", "Henüz makro yok. Kendi tuş kombinasyonunu düğme yap (örn. Ctrl+Shift+Esc, Alt+F4).": "No macros yet. Turn your own key combo into a button (e.g. Ctrl+Shift+Esc, Alt+F4).", "Henüz öneri yok. Arkadaşların oyun oynadıkça burası dolar.": "No suggestions yet. This fills up as your friends play.", "Henüz kayıt yok. Bir arkadaşınla aynı anda oyundayken süre birikir.": "No records yet. Time adds up when you and a friend are in-game together.", "Henüz kayıt yok. Süreler bu program çalışırken oynadığın oyunlar için tutulur.": "No records yet. Time is tracked for games you play while this program runs.", "Arkadaşlarından öneriler": "Suggestions from your friends", "Arkadaşlarının oynadığı ama sende olmayan oyunlar.": "Games your friends play that you don't own.", "Arkadaşlarınla aynı anda oyunda geçirdiğin süre.": "Time you spent in-game at the same time as your friends.", "Birlikte oynama": "Co-play", "Şu an kimse çevrimiçi değil.": "Nobody is online right now.", "Şu an çevrimiçi arkadaşın yok.": "None of your friends are online.", "Şimdi oynanıyor": "Now playing", "Yaklaşan oyun geceleri": "Upcoming game nights", "Bu hafta en çok": "Most this week", "En çok oynananlar": "Most played", "En çok yer kaplayanlar": "Largest on disk", "En uzun oturum:": "Longest session:", "Son 2 haftada": "In the last 2 weeks", "📊 Bu haftan": "📊 Your week", "Günün saatleri": "Hours of the day", "Haftanın günleri": "Days of the week", "Disk alanı": "Disk space", "Hızlı işlemler": "Quick actions", "Yukarı kaydır": "Scroll up", "Aşağı kaydır": "Scroll down", "Klavye: yaz ve Gönder": "Keyboard: type and Send", "Yazıyı gönder": "Send text", "PC'ye mesaj gönder": "Send message to PC", "PC panosuna gönderilecek metin": "Text to send to PC clipboard", "Parmağını kaydır: fare": "Swipe your finger: mouse", "Dokun: sol tık · İki parmakla dokun: sağ tık": "Tap: left click · Two-finger tap: right click", "İki parmakla kaydır: sayfa kaydırma": "Two-finger scroll: page scroll", "Basılı tut": "Hold", "Basılı tut ve konuş": "Hold and talk", "PC mikrofonunu dinle": "Listen to PC mic", "PC'ye konuş": "Talk to PC", "Ölçülüyor…": "Measuring…", "Mağazadaki her şeyi ücretsiz aç": "Unlock everything in the shop for free", "Mağazadaki her şeyi ver": "Grant everything in the shop", "Tüm avatar ve arka planları ücretsiz aç": "Unlock all avatars and backgrounds for free", "Profil vitrini (en fazla 3 rozet)": "Profile showcase (up to 3 badges)", "Jeton (boş bırakırsan değişmez)": "Coins (leave blank to keep)", "Günlük bilgi yarışması": "Daily trivia quiz", "✅ Bugün bitti": "✅ Done today", "✅ Alındı": "✅ Claimed", "✊ Taş-Kağıt-Makas": "✊ Rock-Paper-Scissors", "🎁 Hediye": "🎁 Gift", "🎁 Hediye gönder": "🎁 Send gift", "🎮 Oyuna davet": "🎮 Game invite", "🏆 Sıralama": "🏆 Leaderboard", "📅 Oyun gecesi": "📅 Game night", "📊 Anket": "📊 Poll", "📷 Resim": "📷 Image", "📷 Kendi fotoğrafını avatar yap": "📷 Use your photo as avatar", "Hediye edebileceğin eşya yok.": "You have no items to gift.", "Değişiklikleri gönder": "Send changes", "Anketi gönder": "Send poll", "Geliyorum!": "On my way!", "Geri getir": "Restore", "Listeden gizle": "Hide from list", "Gizlendi. Geri getirmek için Profil sekmesine bak.": "Hidden. Check the Profile tab to restore it.", "Kapak resmi olmayan oyunlar": "Games without cover art", "Kapatma zamanlayıcısı:": "Shutdown timer:", "Yüksek performans güç planı": "High performance power plan", "Giriş yap": "Log in", "Admin oturumu kapandı.": "Logged out of admin.", "Misafir girişi": "Guest login", "Misafir girişi kapatıldı.": "Guest login disabled.", "Misafir oyunu kapatabilsin": "Let guest close games", "Sunucu kapatıldı.": "Server stopped.", "Sıralama henüz gelmedi. Merkez sunucu açıkken yarım saat içinde güncellenir.": "Leaderboard not in yet. It updates within half an hour while the central server is on.", "Ad (örn. OBS)": "Name (e.g. OBS)", "Ad ve klasör gerekli.": "Name and folder required.", "Ad ve yol gerekli.": "Name and path required.", "Başlık yaz.": "Enter a title.", "Soru ve en az 2 seçenek yaz.": "Write a question and at least 2 options.", "Geçersiz tuş. Örn: Ctrl+Shift+Esc": "Invalid key. E.g. Ctrl+Shift+Esc", "Geçmiş bir zaman seçtin.": "You picked a past time.", "Çok kısa, biraz daha uzun basılı tut.": "Too short, hold a bit longer.", "GIF en fazla 2 MB olabilir. Daha küçük bir GIF seç.": "GIF can be at most 2 MB. Pick a smaller GIF.", "En fazla 3 rozet seçebilirsin.": "You can pick at most 3 badges.", "En fazla 8 etiket.": "At most 8 tags.", "Steam API anahtarı girmemiş.": "No Steam API key entered.", "PIN 4–12 rakam olmalı.": "PIN must be 4–12 digits.", "PIN değişti.": "PIN changed.", "Büyük dosyalar gönderilirken ekranı kilitleme, uygulamadan çıkma.": "Don't lock the screen or leave the app while sending large files.", "Dosya başına en fazla 1 GB.": "Up to 1 GB per file.", "PC'de bu klasöre attığın dosyalar burada görünür, \"İndir\"e basınca telefona iner.": "Files you drop into this folder on the PC show up here; tap \"Download\" to get them on your phone.", "Sesli mesaj en fazla ~45 saniye olabilir.": "Voice messages can be up to ~45 seconds.", "Bu tarayıcı desteklemiyor.": "This browser doesn't support it.", "Bu tarayıcı ses aktarımını desteklemiyor.": "This browser doesn't support audio streaming.", "PC sesi açık. Telefon sessizde ise duyulmayabilir.": "PC audio is on. If your phone is on silent you may not hear it.", "Ekran kapatıldı.": "Screen turned off.", "Big Picture açılıyor.": "Opening Big Picture.", "Bu oyun zaten açık.": "This game is already running.", "Önce bir oyun aç.": "Open a game first.", "Önce bir arkadaş ekle ya da oda kur.": "Add a friend or create a room first.", "Önce Sohbet sekmesinden bir arkadaş ekle ya da oda kur.": "Add a friend or create a room from the Chat tab first.", "Önce aşağıdaki Programlar kartına program ekle.": "Add a program to the Programs card below first.", "Rakibinin kabul etmesi bekleniyor…": "Waiting for your opponent to accept…", "Eklendi, ilk yedek alınıyor…": "Added, taking first backup…", "Oda adı:": "Room name:", "Yeni ad:": "New name:", "Not ekle (isteğe bağlı), örn. 10 dk sonra": "Add a note (optional), e.g. in 10 min", "Not ekle (örn. co-op, arkadaşlarla)": "Add a note (e.g. co-op, with friends)", "Oyun seç (isteğe bağlı)": "Pick a game (optional)", "Soru (örn. Bu akşam ne oynayalım?)": "Question (e.g. What should we play tonight?)", "Başlık (örn. Cuma CS2 gecesi)": "Title (e.g. Friday CS2 night)", "Düğme adı (örn. Görev Yöneticisi):": "Button name (e.g. Task Manager):", "Tuşlar (+ ile ayır, örn. Ctrl+Shift+Esc):": "Keys (separate with +, e.g. Ctrl+Shift+Esc):", "Arkadaşının kodu (SR-XXXXX-XXXXX):": "Your friend's code (SR-XXXXX-XXXXX):", "Oda kodu (ODA-XXXX-XXXX):": "Room code (ODA-XXXX-XXXX):", "Örn. Yemek hazır!": "E.g. Dinner's ready!", "Konu adı": "Topic name"},TP={"İyi akşamlar": "Good evening", "İyi geceler": "Good night", "Günaydın": "Good morning", "İyi günler": "Good afternoon", "Merhaba": "Hello", " önce açıldı": " ago", " sa ": " h ", "Son ": "Last ", " gün": " days", " dakika önce": " min ago", " gündür üst üste oynuyorsun.": " days in a row.", " arkadaşında var: ": " friends have it: ", " aynı oyunda": " in the same game", "Son görülme: ": "Last seen: ", "Arkadaşların · ": "Your friends · ", "Seviye ": "Level ", " için oyun": " games to go", " birlikte": " together", " oyunda": " in-game", " jeton": " coins", " oynandı": " played", "Çevrimdışı": "Offline", " kez · ": " times · ", "son: ": "last: ", " oynuyor": " playing", "Bugün": "Today", "Toplam oyun süresi": "Total playtime", "Mağaza eşyası": "Shop items", "Arkadaş": "Friends", " dk": " min"};
const TPK=Object.keys(TP).sort((a,b)=>b.length-a.length);
function trS(s){
  if(LANG!=="en"||!s)return s;
  const k=s.trim();
  if(k&&Object.prototype.hasOwnProperty.call(TX,k))return s.split(k).join(TX[k]);
  let o=s;for(const p of TPK){if(o.indexOf(p)>=0)o=o.split(p).join(TP[p]);}return o;
}
function trNode(n){
  if(LANG!=="en"||!n)return;
  if(n.nodeType===3){const v=n.nodeValue,t=trS(v);if(t!==v)n.nodeValue=t;return;}
  if(n.nodeType!==1)return;
  const A=["placeholder","title","aria-label"];
  const da=el=>{if(!el.getAttribute)return;for(const a of A){const v=el.getAttribute(a);if(v){const t=trS(v);if(t!==v)el.setAttribute(a,t);}}};
  da(n);
  try{const w=document.createTreeWalker(n,NodeFilter.SHOW_TEXT);let x;const L=[];while(x=w.nextNode())L.push(x);for(const tn of L){const v=tn.nodeValue,t=trS(v);if(t!==v)tn.nodeValue=t;}}catch(e){}
  if(n.querySelectorAll)n.querySelectorAll("[placeholder],[title],[aria-label]").forEach(da);
}
try{["alert","confirm","prompt"].forEach(fn=>{const o=window[fn].bind(window);window[fn]=(m,...r)=>o(LANG==="en"?trS(String(m==null?"":m)):m,...r);});}catch(e){}
try{new MutationObserver(ms=>{if(LANG!=="en")return;for(const m of ms){if(m.type==="characterData"){const v=m.target.nodeValue,t=trS(v);if(t!==v)m.target.nodeValue=t;}else{for(const nd of m.addedNodes)trNode(nd);}}}).observe(document.documentElement,{childList:true,subtree:true,characterData:true});}catch(e){}
try{if(LANG==="en")trNode(document.body);}catch(e){}
let pin="",games=[],running={appid:"0",name:""},tab="games",flt="all",since=0,sysT=0,perfT=0;
try{pin=localStorage.getItem("pin")||""}catch(e){}
const H=()=>({"X-Pin":pin,"Content-Type":"application/json"});
const post=(p,b)=>fetch(p,{method:"POST",headers:H(),body:JSON.stringify(b||{})});
const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");
const rel=t=>{if(!t)return"Hiç oynanmadı";const d=(Date.now()/1e3-t)/86400;return d<1?"Bugün":d<2?"Dün":d<30?Math.floor(d)+" gün önce":d<365?Math.floor(d/30)+" ay önce":"Uzun zaman önce"};
const IMGS=id=>[`https://cdn.cloudflare.steamstatic.com/steam/apps/${id}/header.jpg`,`https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${id}/header.jpg`,`https://cdn.cloudflare.steamstatic.com/steam/apps/${id}/capsule_616x353.jpg`,`https://cdn.cloudflare.steamstatic.com/steam/apps/${id}/library_hero.jpg`];
const img=id=>IMGS(id)[0];
function toast(m,k){const t=$("#toast");t.textContent=m;t.className="show "+(k||"");clearTimeout(toast.t);toast.t=setTimeout(()=>t.className="",3000)}
let _ac=null;
function sfx(type){
  if(pref("sfx","1")!=="1")return;
  try{const AC=window.AudioContext||window.webkitAudioContext;if(!AC)return;_ac=_ac||new AC();const c=_ac;if(c.state==="suspended")c.resume();
    const seq={coin:[[880,0,.07],[1320,.07,.12]],win:[[523,0,.1],[659,.1,.1],[784,.2,.2]],buy:[[660,0,.08],[990,.08,.15]],msg:[[700,0,.08]],badge:[[523,0,.1],[784,.1,.1],[1047,.2,.25]],spin:[[400,0,.5]]}[type]||[[700,0,.08]];
    seq.forEach(([f,t,d])=>{const o=c.createOscillator(),g=c.createGain();o.type=type==="spin"?"sawtooth":"sine";o.frequency.setValueAtTime(f,c.currentTime+t);if(type==="spin")o.frequency.exponentialRampToValueAtTime(900,c.currentTime+t+d);g.gain.setValueAtTime(.0001,c.currentTime+t);g.gain.exponentialRampToValueAtTime(.18,c.currentTime+t+.01);g.gain.exponentialRampToValueAtTime(.0001,c.currentTime+t+d);o.connect(g);g.connect(c.destination);o.start(c.currentTime+t);o.stop(c.currentTime+t+d+.02)})}catch(e){}
}

function loginView(err){
  $("#app").innerHTML=`<div class="center"><h1>Oyunlarım</h1><p>${err||"PC'de belirlediğin PIN kodunu gir."}</p>
  <input id="pin" type="password" inputmode="numeric" autocomplete="off" placeholder="PIN"><button class="primary" id="go">Giriş yap</button></div>`;
  $("#go").onclick=()=>{pin=$("#pin").value;load()};
  $("#pin").onkeydown=e=>{if(e.key==="Enter")$("#go").click()};
}
async function load(){
  try{
    const r=await fetch("/api/games",{headers:H()});
    if(r.status===401){try{localStorage.removeItem("pin")}catch(e){};return loginView(pin?"PIN yanlış. Tekrar dene.":"")}
    games=await r.json();try{localStorage.setItem("pin",pin)}catch(e){}
    let who={role:"full"};try{who=await(await fetch("/api/whoami",{headers:H()})).json()}catch(e){}
    if(who.role==="guest"){guestView(who);return}
    await meLoad();
    try{if(!localStorage.getItem("lang")&&ME&&ME.lang){LANG=ME.lang;localStorage.setItem("lang",LANG)}}catch(e){}
    shell();poll();
  }catch(e){loginView("PC'ye ulaşılamadı. Programın çalıştığından ve (evin dışındaysan) Tailscale'in açık olduğundan emin ol.")}
}
function shell(){
  $("#app").innerHTML=`<div id="main"></div><nav><button data-t="home">Özet</button><button data-t="games">Oyunlar</button>${ME.standalone?"":'<button data-t="chat">Sohbet<b class="nb" id="nbc" hidden></b></button>'}<button data-t="pc">PC</button><button data-t="files">Dosyalar</button><button data-t="set">Ayarlar</button></nav>`;chatBadge();
  document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{tab=b.dataset.t;show();if(tab==="games")refreshGames()});tab=pref("tab","home");show();
}
function show(){
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.t===tab||(tab==="profile"&&b.dataset.t==="home")||(tab==="steam"&&b.dataset.t==="set")));
  clearInterval(sysT);clearInterval(perfT);liveOn=false;if(aud)aud.stop();if(tpHeld)mouseUp();if(pttRec)pttRec.cancel();window.scrollTo(0,0);document.body.classList.remove("inchat");chatOpen=null;
  ({home:homeView,games:gamesView,chat:chatView,pc:pcView,files:filesView,profile:profileView,steam:steamView,set:settingsView}[tab]||homeView)();
}

/* ---- Oyunlar ---- */
function gamesView(){
  $("#main").innerHTML=`<header><div class="row" style="margin:0 0 10px;flex-wrap:wrap;gap:6px"><h1 style="margin:0">Oyunlarım</h1><span class="sp"></span>${ME.standalone?"":'<button class="chip" id="discb">Keşfet</button>'}<button class="chip" id="wlb">İstek listesi</button><button class="chip" id="libb">Kur / Güncelle</button></div>
  <div id="now"><div><small id="nowt">Şimdi oynanıyor</small><b id="nowname"></b></div><button class="danger" id="stop">Kapat</button></div>
  <input id="q" type="search" placeholder="Oyun ara" autocomplete="off">
  <div class="row"><button class="chip" data-f="all">Tümü</button><button class="chip" data-f="fav">Favoriler</button><button class="chip" id="srt"></button><span class="sp"></span><button class="chip" id="rnd">Rastgele</button></div><div class="row" id="tagrow"></div></header>
  <div id="hero"></div><div id="strips"></div><div class="grid" id="grid"></div>`;
  $("#libb").onclick=openLib;$("#wlb").onclick=()=>openWl();(()=>{const _d=$("#discb");if(_d)_d.onclick=openDiscover})();$("#q").oninput=render;$("#stop").onclick=stopGame;$("#rnd").onclick=rand;$("#srt").onclick=()=>{sort=({recent:"az",az:"size",size:"recent"})[sort];render()};
  document.querySelectorAll("[data-f]").forEach(b=>b.onclick=()=>{flt=b.dataset.f;render()});
  render();showNow();
}
function render(){
  const q=($("#q").value||"").toLowerCase().trim();
  document.querySelectorAll("[data-f]").forEach(b=>b.classList.toggle("on",b.dataset.f===flt));
  const tgs=allTags();if(flt.startsWith("tag:")&&!tgs.includes(flt.slice(4)))flt="all";
  const tr=$("#tagrow");tr.innerHTML=tgs.map(t=>`<button class="chip${flt==="tag:"+t?" on":""}" data-tg="${esc(t)}">#${esc(t)}</button>`).join("");
  tr.querySelectorAll("[data-tg]").forEach(b=>b.onclick=()=>{flt=flt==="tag:"+b.dataset.tg?"all":"tag:"+b.dataset.tg;render()});
  document.querySelectorAll("[data-f]").forEach(b=>b.classList.toggle("on",b.dataset.f===flt));
  let list=games.filter(g=>g.name.toLowerCase().includes(q)&&(flt==="all"||(flt==="fav"?g.fav:(g.tags||[]).includes(flt.slice(4))))&&!g.hidden);
  if(sort==="az")list.sort((a,b)=>a.name.localeCompare(b.name,"tr"));if(sort==="size")list.sort((a,b)=>b.size-a.size);
  $("#srt").textContent=({recent:"Son oynanan",az:"A–Z",size:"Boyut"})[sort];
  const top=games.find(g=>g.last>0&&!g.hidden);
  const hero=$("#hero");
  if(!q&&flt==="all"&&sort==="recent"&&top&&running.appid==="0"&&pref("hero","1")==="1"){
    hero.innerHTML=`<button class="hero" data-id="${top.appid}"><img alt="" src="${img(top.appid)}"><div class="cap"><small>Kaldığın yerden devam et</small><b>${esc(top.name)}</b></div><span class="go">Oyna</span></button>`;
    hero.firstChild.onclick=e=>launch(top.appid,top.name,e.currentTarget);
    list=list.filter(g=>g.appid!==top.appid);
  }else hero.innerHTML="";
  const sp=$("#strips"),hv=!!hero.innerHTML;
  const mk=(t,a)=>a.length?`<h2 style="margin:14px 0 8px">${t}</h2><div class="strip">${a.map(g=>`<button class="mini" data-id="${g.appid}"><img alt="" src="${img(g.appid)}"><span>${esc(g.name)}</span></button>`).join("")}</div>`:"";
  if(!q&&flt==="all"&&sort==="recent"&&pref("strips","1")==="1"){
    const rc=games.filter(g=>g.last>0&&!g.hidden&&!(hv&&g.appid===top.appid)).slice(0,8);
    sp.innerHTML=mk("Son oynananlar",rc)+mk("Favoriler",games.filter(g=>g.fav&&!g.hidden));
    sp.querySelectorAll(".mini").forEach(b=>b.onclick=()=>launch(b.dataset.id,b.querySelector("span").textContent,b));
  }else sp.innerHTML="";
  const grid=$("#grid");
  if(!list.length&&!hero.innerHTML){grid.className="";grid.innerHTML=`<div class="empty">${flt==="fav"?"Henüz favori yok. Bir oyunun köşesindeki yıldıza dokun.":games.length?"Eşleşen oyun yok.":"Kurulu oyun bulunamadı."}</div>`;return}
  grid.className="grid";
  grid.innerHTML=list.map(g=>`<div style="position:relative"><button class="game${g.appid===running.appid?" live":""}" data-id="${g.appid}">
  <img loading="lazy" alt="" src="${img(g.appid)}"><span>${esc(g.name)}</span><em${g.note?' style="color:var(--accent)"':""}>${g.note?esc(g.note):sort==="size"?(g.size/2**30).toFixed(1)+" GB":rel(g.last)}</em>${g.tags&&g.tags.length?`<small class="tg">${g.tags.map(t=>"#"+esc(t)).join(" ")}</small>`:""}</button>
  <button class="star${g.fav?" on":""}" data-s="${g.appid}" aria-label="Favori">${g.fav?"★":"☆"}</button></div>`).join("");
  grid.querySelectorAll(".game").forEach(b=>{b.onclick=()=>launch(b.dataset.id,b.querySelector("span").textContent,b);lp(b,b.dataset.id)});
  grid.querySelectorAll(".star").forEach(b=>b.onclick=()=>fav(b.dataset.s));
}
function allTags(){return[...new Set(games.flatMap(g=>g.tags||[]))].sort((a,b)=>a.localeCompare(b,"tr"))}
const TAG_PRESETS=["Arkadaşlarla","Tek kişilik","Bitirdim","Sonra oynanacak","Rekabetçi","Rahatlatıcı"];
async function fav(id){
  const g=games.find(x=>x.appid===id);g.fav=!g.fav;render();
  try{await post("/api/fav",{appid:id,fav:g.fav})}catch(e){toast("Favori kaydedilemedi.","err")}
}
function rand(){
  const pool=games.filter(g=>g.appid!==running.appid&&!g.hidden);if(!pool.length)return;
  const g=pool[Math.floor(Math.random()*pool.length)];
  if(confirm(`Şansına: ${g.name}\nBu oyun açılsın mı?`))launch(g.appid,g.name,null,true);
}
function fmt(s){const m=Math.floor(s/60);return m<60?m+" dk":Math.floor(m/60)+" sa "+(m%60)+" dk"}
function showNow(){
  const n=$("#now");if(!n)return;
  n.classList.toggle("on",running.appid!=="0");
  $("#nowname").textContent=running.name||"Bilinmeyen oyun";
  $("#nowt").textContent="Şimdi oynanıyor · "+fmt((Date.now()-since)/1e3)+(running.kill_in?" · "+Math.ceil(running.kill_in/60)+" dk sonra kapanır":"");
  document.querySelectorAll(".game").forEach(b=>b.classList.toggle("live",b.dataset.id===running.appid));
}
setInterval(()=>{if(tab==="games")showNow()},20000);
async function poll(){
  try{const r=await fetch("/api/status",{headers:H()});if(r.ok){const s=await r.json();
    const ch=s.appid!==running.appid;running=s;since=Date.now()-s.elapsed*1e3;const gi=$("#gtinfo");if(gi)gi.textContent=s.kill_in?"Oyun "+Math.ceil(s.kill_in/60)+" dk sonra kapanacak.":"Zamanlayıcı kapalı.";
    if(tab==="games"&&$("#now")){showNow();if(ch)render()}}}catch(e){}
}
setInterval(()=>{if(!document.hidden&&games.length)poll()},5000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&games.length)poll()});
async function launch(id,name,el,skip){
  if(running.appid===id){toast("Bu oyun zaten açık.");return}
  if(running.appid!=="0"&&!skip&&!confirm(`${running.name||"Çalışan oyun"} kapatılıp ${name} açılsın mı?\n\nKaydedilmemiş ilerleme kaybolabilir.`))return;
  if(el)el.classList.add("busy");
  toast(running.appid!=="0"?"Önceki oyun kapatılıyor…":"Başlatılıyor…");
  try{const r=await post("/api/launch",{appid:id});
    if(r.ok){toast("PC'de başlatılıyor: "+name,"ok");setTimeout(poll,6000);setTimeout(poll,15000)}else toast("Başlatılamadı.","err")}
  catch(e){toast("PC'ye ulaşılamadı.","err")}
  setTimeout(()=>el&&el.classList.remove("busy"),1500);
}
async function stopGame(){
  if(!confirm(`${running.name||"Oyun"} kapatılsın mı?\n\nKaydedilmemiş ilerleme kaybolabilir.`))return;
  toast("Kapatılıyor…");
  try{const r=await post("/api/stop");toast(r.ok?"Oyun kapatıldı.":"Kapatılamadı.",r.ok?"ok":"err")}catch(e){toast("PC'ye ulaşılamadı.","err")}
  poll();
}

/* ---- Bilgisayar ---- */
const PW={lock:["Kilitle",""],sleep:["Uyut","Uyuyan PC'ye bağlanamazsın, elle uyandırman gerekir."],restart:["Yeniden başlat","PC açılınca Windows'a giriş yapman gerekebilir."],shutdown:["Kapat","Kapanan PC'yi uzaktan açamazsın."]};
function pcView(){
  $("#main").innerHTML=`<header><h1>Bilgisayar</h1></header>
  <div class="card"><h2>Durum</h2><div id="stats"><div class="empty">Yükleniyor…</div></div></div>
  <div class="card"><h2>Canlı performans</h2><div class="pf" id="pf"><div class="empty" style="padding:10px 0;grid-column:1/-1">Ölçülüyor…</div></div><p class="note" id="pfn" style="margin-top:8px"></p></div>
  <div class="card"><h2>Ses & medya</h2><div id="nowp"></div><div class="btns"><button class="btn" data-v="down">Kıs</button><button class="btn" data-v="mute">Sessiz</button><button class="btn" data-v="up">Aç</button></div>
  <div class="btns" style="margin-top:8px"><button class="btn" data-v="prev">⏮ Önceki</button><button class="btn" data-v="playpause">⏯ Oynat / Duraklat</button><button class="btn" data-v="next">⏭ Sonraki</button></div>
  <div id="adev" style="margin-top:12px"></div><div id="avol" style="margin-top:6px"></div></div>
  <div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0;flex:1">Makro / kısayol düğmeleri</h2><button class="chip" id="mkadd">+ Ekle</button></div><div id="mks"><p class="note">Yükleniyor…</p></div></div>
  <div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0;flex:1">Otomatik yedekleme</h2><button class="chip" id="bkadd">+ Klasör</button></div><p class="note" style="margin-bottom:10px">Oyun kayıtlarını (save) ya da istediğin klasörü düzenli yedekler. Yedekler PC'de "Yedekler" klasörüne zip olarak kaydedilir.</p><div id="bks"><p class="note">Yükleniyor…</p></div></div>
  <div class="card"><h2>Ekran</h2>
  <div class="btns"><button class="btn" id="shotbtn">Tek kare</button><button class="btn" id="live">Canlı izle</button></div>
  <div class="row" style="flex-wrap:wrap"><button class="chip" data-w="480">Düşük</button><button class="chip on" data-w="720">Orta</button><button class="chip" data-w="1100">Yüksek</button><span class="sp"></span><button class="chip" id="aud">Ses</button><button class="chip" id="fs">Tam ekran</button></div>
  <div class="row"><button class="chip" id="ctl">Dokunarak kontrol</button></div>
  <div class="btns" id="keys" hidden style="margin-top:8px">${[["enter","Enter"],["esc","Esc"],["backspace","Sil"],["space","Boşluk"],["left","←"],["up","↑"],["down","↓"],["right","→"]].map(k=>`<button class="btn" data-k="${k[0]}">${k[1]}</button>`).join("")}</div>
  <div id="ctlx" hidden style="margin-top:8px"><div class="row" style="margin:0 0 8px;flex-wrap:wrap">${[["alttab","Alt+Tab"],["altf4","Alt+F4"],["win","Win"],["windesk","Masaüstü"],["f11","F11"],["taskmgr","Görev Yön."]].map(k=>`<button class="chip" data-cb="${k[0]}">${k[1]}</button>`).join("")}</div><div class="row" style="margin:0 0 8px;flex-wrap:wrap"><button class="chip" id="rc">Sağ tık</button><button class="chip" id="su">Yukarı kaydır</button><button class="chip" id="sd">Aşağı kaydır</button></div>
  <input type="text" id="typ" placeholder="PC'ye yaz…"><button class="btn" style="width:100%" id="typb">Yazıyı gönder</button></div>
  <img id="shot" alt="PC ekranı" hidden><button class="chip on" id="fsx">Kapat</button></div>
  <div class="card"><h2>Monitör</h2><div id="mons"><p class="note">Yükleniyor…</p></div>
  <p class="note" style="margin:4px 0 6px">Gece ışığı (mavi ışığı azaltır)</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 10px">${[["0","Kapalı"],["1","Hafif"],["2","Orta"],["3","Güçlü"],["4","En sıcak"]].map(([v,l])=>`<button class="chip" data-wm="${v}">${l}</button>`).join("")}</div>
  <div class="btns"><button class="btn" id="moff">Ekranı kapat</button><button class="btn" id="mnl">Windows gece ışığı ayarı</button></div></div>
  <div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0">Görev yöneticisi</h2><span class="sp"></span><button class="chip" id="tmr">Yenile</button></div><div id="tm"><button class="btn" style="width:100%" id="tmo">Çalışan programları göster</button></div></div>
  <div class="card"><h2>İnterkom</h2>
  <p class="note" style="margin-bottom:10px">Basılı tutup konuş, bırakınca PC hoparlöründen çalar. PC'nin mikrofonunu da buradan dinleyebilirsin.</p>
  <div id="pttw"></div>
  <button class="btn" style="width:100%;margin-top:8px" id="micl">PC mikrofonunu dinle</button></div>
  <div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0">Touchpad</h2><span class="sp"></span>${[["1","Yavaş"],["2","Normal"],["3.5","Hızlı"]].map(([v,l])=>`<button class="chip${pref("tps","2")===v?" on":""}" data-tps="${v}">${l}</button>`).join("")}</div>
  <div id="tp" class="tp">Parmağını kaydır: fare<br>Dokun: sol tık · İki parmakla dokun: sağ tık<br>İki parmakla kaydır: sayfa kaydırma</div>
  <div class="btns" style="margin-top:8px;grid-template-columns:repeat(3,1fr)"><button class="btn" data-mb="left">Sol tık</button><button class="btn" data-mb="right">Sağ tık</button><button class="btn" id="tpd">Basılı tut</button></div>
  <input type="text" id="tpk" placeholder="Klavye: yaz ve Gönder'e bas" autocomplete="off" style="margin:8px 0 8px"><div class="btns" style="grid-template-columns:repeat(3,1fr)"><button class="btn" id="tpks">Gönder</button><button class="btn" data-tk="backspace">Sil ⌫</button><button class="btn" data-tk="enter">Enter ↵</button></div></div>
  <div class="card"><h2>Steam</h2><div class="btns">${[["bigpicture","Big Picture"],["store","Mağaza"],["friends","Arkadaşlar"],["downloads","İndirmeler"],["library","Kütüphane"],["exit","Steam'i kapat"]].map(k=>`<button class="btn${k[0]==="exit"?" bad":""}" data-st="${k[0]}">${k[1]}</button>`).join("")}</div></div>
  <div class="card"><h2>Discord</h2><div class="btns"><button class="btn" data-dc="mute">Mikrofon</button><button class="btn" data-dc="deafen">Sağırlaştır</button><button class="btn" data-dc="open">Discord'u aç</button></div>
  <p class="note" style="margin:8px 0 12px">Mikrofon ve sağırlaştırma Ctrl+Shift+M / Ctrl+Shift+D tuşlarını gönderir. Oyun açıkken de çalışması için Discord > Ayarlar > Tuş Atamaları'na bu iki kısayolu ekle.</p>
  <p class="note" style="margin:0 0 6px">Arkadaşlarını çağır (Ayarlar'da webhook ekli olmalı):</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 8px">${["Gelen var mı?","5 dakikaya giriyorum","Bu akşam oynayalım mı?","Lobi kuruyorum, gelin"].map(t=>`<button class="chip" data-dq="${t}">${t}</button>`).join("")}</div>
  <input type="text" id="dcm" maxlength="300" placeholder="Discord kanalına mesaj"><button class="btn" style="width:100%" id="dcs">Discord'a gönder</button></div>
  <div class="card" id="appsc" hidden><h2>Programlar</h2><div class="btns" id="appb"></div></div>
  <div class="card"><h2>Pano köprüsü</h2><input type="text" id="cb" placeholder="PC panosuna gönderilecek metin"><div class="btns"><button class="btn" id="cbs">PC'ye gönder</button><button class="btn" id="cbg">PC'den al</button></div><div class="note" id="cbo" style="margin-top:8px;word-break:break-word"></div></div>
  <div class="card"><h2>Oyun zamanlayıcısı</h2>
  <p class="note" id="gtinfo" style="margin-bottom:10px">Zamanlayıcı kapalı.</p>
  <div class="row" style="margin:0"><button class="chip" data-gt="30">30 dk</button><button class="chip" data-gt="60">1 sa</button><button class="chip" data-gt="120">2 sa</button><button class="chip" data-gt="0">İptal</button></div></div>
  <div class="card"><h2>PC'ye mesaj gönder</h2>
  <input type="text" id="msg" maxlength="200" placeholder="Örn. Yemek hazır!"><button class="btn" style="width:100%" id="msgbtn">Ekranda göster</button></div>
  <div class="card"><h2>Kestirmeler (iPhone widget)</h2>
  <p class="note" style="margin-bottom:10px">Bir komut seç, bağlantıyı kopyala. iPhone'daki Kestirmeler uygulamasında yeni kestirme oluştur, "URL İçeriğini Al" eylemini ekle ve bağlantıyı yapıştır. Sonra ana ekrana Kestirmeler widget'ı ekleyip bu kestirmeyi seç.</p>
  <select id="qk"></select><input type="text" id="qyt" hidden placeholder="İsteğe bağlı: YouTube'da ara (örn. lofi müzik)" autocomplete="off"><input id="qurl" readonly><button class="btn" style="width:100%" id="qcopy">Bağlantıyı kopyala</button>
  <p class="note" style="margin:14px 0 6px">Hazır widget'lar · dokun, bağlantı kopyalansın. Kestirme oluştururken simge ve rengini de seçebilirsin.</p><div class="wgal" id="wgal"></div></div>
  <div class="card"><h2>Güç</h2><div class="btns">${Object.keys(PW).map(k=>`<button class="btn${k==="restart"||k==="shutdown"?" bad":""}" data-p="${k}">${PW[k][0]}</button>`).join("")}</div>
  <div class="row" style="flex-wrap:wrap;margin-top:12px"><span class="note">Kapatma zamanlayıcısı:</span><button class="chip" data-tm="30">30 dk</button><button class="chip" data-tm="60">1 sa</button><button class="chip" data-tm="120">2 sa</button><button class="chip" data-tm="0">İptal</button></div></div>
  <button class="btn" style="width:100%" id="out">Çıkış yap</button>`;
  document.querySelectorAll("[data-v]").forEach(b=>b.onclick=()=>post("/api/volume",{action:b.dataset.v}));
  document.querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>power(b.dataset.p));
  $("#shotbtn").onclick=shot;$("#live").onclick=live;$("#fs").onclick=()=>{$("#shot").hidden||$("#shot").classList.add("full")};
  $("#fsx").onclick=()=>$("#shot").classList.remove("full");
  document.querySelectorAll("[data-w]").forEach(b=>b.onclick=()=>{qw=+b.dataset.w;document.querySelectorAll("[data-w]").forEach(x=>x.classList.toggle("on",x===b))});
  $("#ctl").onclick=e=>{ctl=!ctl;e.currentTarget.classList.toggle("on",ctl);$("#keys").hidden=!ctl;$("#ctlx").hidden=!ctl};
  document.querySelectorAll("[data-k]").forEach(b=>b.onclick=()=>post("/api/key",{key:b.dataset.k}));
  $("#shot").onclick=tap;fillQuick();
  $("#rc").onclick=e=>{rc=!rc;e.currentTarget.classList.toggle("on",rc)};
  document.querySelectorAll("[data-cb]").forEach(b=>b.onclick=()=>post("/api/combo",{key:b.dataset.cb}));$("#su").onclick=()=>post("/api/scroll",{d:1});$("#sd").onclick=()=>post("/api/scroll",{d:-1});
  $("#typb").onclick=async()=>{const t=$("#typ").value;if(!t)return;await post("/api/type",{text:t});$("#typ").value="";toast("Yazıldı.","ok")};
  document.querySelectorAll("[data-tm]").forEach(b=>b.onclick=()=>timer(+b.dataset.tm));
  document.querySelectorAll("[data-gt]").forEach(b=>b.onclick=()=>gameTimer(+b.dataset.gt));
  loadApps();clipBind();
  $("#msgbtn").onclick=async()=>{const t=$("#msg").value.trim();if(!t)return;try{await post("/api/message",{text:t});$("#msg").value="";toast("Mesaj PC ekranında gösterildi.","ok")}catch(e){toast("PC'ye ulaşılamadı.","err")}};
  poll();document.querySelectorAll("[data-st]").forEach(b=>b.onclick=async()=>{if(b.dataset.st==="exit"&&!confirm("Steam kapatılsın mı?\n\nÇalışan oyun da kapanabilir."))return;await post("/api/steam",{action:b.dataset.st});toast("Komut gönderildi.","ok")});
  $("#out").onclick=()=>{try{localStorage.removeItem("pin")}catch(e){};pin="";games=[];loginView()};
  stats();sysT=setInterval(stats,3000);perf();perfT=setInterval(perf,2000);
  $("#aud").onclick=audioToggle;tpBind();monBind();tmBind();pttBind();audioCards();mkBind();bkBind();
  document.querySelectorAll("[data-dc]").forEach(b=>b.onclick=async()=>{try{const r=await post("/api/discord",{action:b.dataset.dc});toast(r.ok?(b.dataset.dc==="open"?"Discord açılıyor.":"Kısayol gönderildi."):"Discord açılamadı.",r.ok?"ok":"err")}catch(e){toast("PC'ye ulaşılamadı.","err")}});
  document.querySelectorAll("[data-dq]").forEach(b=>b.onclick=()=>dcSay(b.dataset.dq));
  $("#dcs").onclick=async()=>{const t=$("#dcm").value.trim();if(!t)return;if(await dcSay(t))$("#dcm").value=""};
}
async function dcSay(t){
  try{const j=await(await post("/api/discord",{action:"say",text:t})).json();
    if(j.ok){toast("Discord'a gönderildi.","ok");return true}
    toast(j.error==="webhook"?"Önce Ayarlar'da Discord webhook'unu ekle.":"Gönderilemedi.","err")}catch(e){toast("PC'ye ulaşılamadı.","err")}
  return false;
}
function spark(a,mx){
  a=a.filter(v=>v!=null);if(a.length<2)return"";const m=mx||Math.max(...a,1),n=a.length;
  return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke" points="${a.map((v,i)=>(i/(n-1)*100).toFixed(1)+","+(27-Math.min(v/m,1)*25).toFixed(1)).join(" ")}"/></svg>`;
}
async function perf(){
  const el=$("#pf");if(!el)return;
  try{const r=await fetch("/api/perf",{headers:H()});if(!r.ok)return;const p=await r.json();if(!$("#pf")||!p.now)return;
    const h=p.hist||[],c=p.now,col=k=>h.map(x=>x[k]);
    const T=(l,k,u,mx,warn)=>c[k]==null?"":`<div class="pt"><span>${l}</span><b${warn&&c[k]>=warn?` style="color:var(${c[k]>=warn+10?"--bad":"--warn"})"`:""}>${c[k]}${u}</b>${spark(col(k),mx)}</div>`;
    el.innerHTML=T("FPS","fps","",0)+T("İşlemci","cpu","%",100)+T("İşlemci sıcaklığı","ct","°C",100,80)+T("Ekran kartı","gu","%",100)+T("Ekran kartı sıcaklığı","gt","°C",100,80)+T("Ekran kartı belleği","vm","%",100)+T("Ekran kartı gücü","gw"," W",0)+T("Bellek","ram","%",100);
    const miss=[];if(c.fps==null)miss.push(running.appid!=="0"?"FPS için MSI Afterburner (RivaTuner) açık olmalı.":"FPS oyun açıkken görünür (MSI Afterburner / RivaTuner gerekir).");
    if(c.ct==null)miss.push("İşlemci sıcaklığı için LibreHardwareMonitor'ı yönetici olarak açık tut.");if(c.gu==null)miss.push("Ekran kartı bilgisi için NVIDIA sürücüsü gerekir.");
    $("#pfn").textContent=miss.join(" ");
  }catch(e){}
}
async function stats(){
  const el=$("#stats");if(!el)return;
  try{const r=await fetch("/api/system",{headers:H()});if(!r.ok)return;const s=await r.json();
    const bar=(n,v,x)=>`<div class="bar"><div><span>${n}</span><i>${x}</i></div><p><b style="width:${Math.min(v,100)}%"></b></p></div>`;
    el.innerHTML=bar("İşlemci",s.cpu,s.cpu+"%")+bar("Bellek",s.ram,s.ram_used+" / "+s.ram_total+" GB")+
      (s.gpu?bar("Ekran kartı",s.gpu[0],s.gpu[0]+"% · "+s.gpu[1]+"°C"):"")+(s.dl&&s.dl.length?s.dl.map(d=>bar("İndiriliyor: "+esc(d.name),d.pct,d.pct+"%")).join(""):"")+`<p class="note" style="margin:10px 0 0">PC ${upt(s.uptime)} önce açıldı.</p>`;
    const np=$("#nowp");if(np){const m=s.media;np.innerHTML=m?`<div class="np"><div class="ic">${m.playing?"🎵":"⏸"}</div><div><b>${esc(m.title)}</b><span>${esc(m.artist||m.src||"")}${m.playing?"":" · duraklatıldı"}</span></div></div>`:""}
  }catch(e){}
}
async function gameTimer(m){
  if(running.appid==="0"){toast("Önce bir oyun aç.","err");return}
  if(m&&!confirm(`${running.name||"Oyun"} ${m>=60?m/60+" saat":m+" dakika"} sonra kapatılsın mı?\n\nKaydedilmemiş ilerleme kaybolabilir.`))return;
  try{await post("/api/gametimer",{minutes:m});toast(m?"Oyun zamanlayıcısı kuruldu.":"Zamanlayıcı iptal edildi.","ok");poll()}catch(e){toast("PC'ye ulaşılamadı.","err")}
}
async function timer(m){
  if(m&&!confirm(`PC ${m>=60?m/60+" saat":m+" dakika"} sonra kapansın mı?\n\nİptal etmek için aynı yerden İptal'e dokun.`))return;
  try{await post("/api/timer",m?{minutes:m}:{cancel:true,minutes:1});toast(m?"Kapatma zamanlayıcısı kuruldu.":"Planlı kapatma iptal edildi.","ok")}
  catch(e){toast("PC'ye ulaşılamadı.","err")}
}
async function refreshGames(){
  try{const r=await fetch("/api/games",{headers:H()});if(r.ok){games=await r.json();if(tab==="games"&&$("#grid"))render()}}catch(e){}
}
function fillQuick(){
  const o=[["stop","Çalışan oyunu kapat"],["bigpicture","Big Picture aç"],["youtube","YouTube'u aç"],["up","Sesi aç"],["down","Sesi kıs"],["mute","Sessiz"],["lock","PC'yi kilitle"],["last","Son oynadığımı aç"],["random","Rastgele oyun aç"],["sleep","PC'yi uyut"],["shutdown","PC'yi kapat"],["timer&m=30","PC'yi 30 dk sonra kapat"],["timer&m=60","PC'yi 1 saat sonra kapat"],["cancel","Planlı kapatmayı iptal et"]];
  const sel=$("#qk");
  sel.innerHTML=`<optgroup label="Oyunu aç">`+games.map(g=>`<option value="launch&id=${g.appid}">${esc(g.name)}</option>`).join("")+`</optgroup><optgroup label="Diğer">`+o.map(x=>`<option value="${x[0]}">${x[1]}</option>`).join("")+`</optgroup>`;
  const upd=()=>{const yt=sel.value==="youtube",t=yt?($("#qyt").value||"").trim():"";$("#qyt").hidden=!yt;
    $("#qurl").value=`${location.origin}/q?pin=${encodeURIComponent(pin)}&do=${sel.value}${t?"&q="+encodeURIComponent(t):""}`};
  sel.onchange=upd;$("#qyt").oninput=upd;upd();
  const last=games.filter(g=>g.last).sort((a,b)=>b.last-a.last)[0];
  const presets=[["🎮","Son oyunu aç","last"],["🎲","Rastgele oyun","random"],["⏹","Oyunu kapat","stop"],["🎵","Spotify / medya","youtube"],["🔇","PC'yi sustur","mute"],["🔒","PC'yi kilitle","lock"],["😴","PC'yi uyut","sleep"],["🕹","Big Picture","bigpicture"],["⏰","30 dk sonra kapat","timer&m=30"],["🖥","Ekranı kapat","mute"]];
  if(last)presets.unshift(["▶️",last.name.length>11?last.name.slice(0,11)+"…":last.name,"launch&id="+last.appid]);
  const wg=$("#wgal");if(wg){wg.innerHTML=presets.map((x,i)=>`<button data-wp="${i}"><b>${x[0]}</b>${esc(x[1])}</button>`).join("");
    wg.querySelectorAll("[data-wp]").forEach(b=>b.onclick=()=>{const x=presets[+b.dataset.wp];copyText(`${location.origin}/q?pin=${encodeURIComponent(pin)}&do=${x[2]}`);sfx("msg")})}
  $("#qcopy").onclick=()=>{const i=$("#qurl");i.select();i.setSelectionRange(0,999);try{document.execCommand("copy");toast("Bağlantı kopyalandı.","ok")}catch(e){toast("Bağlantıyı basılı tutup kopyala.","")}};
}
async function frame(){
  const r=await fetch("/api/screenshot?w="+qw,{headers:H()});if(!r.ok)throw 0;
  const u=URL.createObjectURL(await r.blob()),im=$("#shot");if(!im)return;
  const old=im.src;im.src=u;im.hidden=false;if(old&&old.startsWith("blob:"))URL.revokeObjectURL(old);
}
async function shot(){
  const b=$("#shotbtn");b.textContent="Alınıyor…";
  try{await frame()}catch(e){toast("Ekran görüntüsü alınamadı.","err")}
  if($("#shotbtn"))$("#shotbtn").textContent="Tek kare";
}
async function live(){
  if(liveOn){liveOn=false;return}
  liveOn=true;$("#live").textContent="Durdur";
  while(liveOn&&tab==="pc"){try{await frame()}catch(e){toast("Ekran alınamadı, canlı yayın durdu.","err");break}}
  liveOn=false;if($("#live"))$("#live").textContent="Canlı izle";
}
function tap(e){
  if(!ctl)return;
  const im=e.currentTarget,r=im.getBoundingClientRect(),ar=im.naturalWidth/im.naturalHeight;
  let w=r.width,h=r.height,ox=0,oy=0;
  if(w/h>ar){w=h*ar;ox=(r.width-w)/2}else{h=w/ar;oy=(r.height-h)/2}
  const x=(e.clientX-r.left-ox)/w,y=(e.clientY-r.top-oy)/h;
  if(x>=0&&x<=1&&y>=0&&y<=1)post("/api/click",{x,y,right:rc});
}
function power(k){
  if(!confirm(`${PW[k][0]}?${PW[k][1]?"\n\n"+PW[k][1]:""}`))return;
  post("/api/power",{action:k});toast(PW[k][0]+" komutu gönderildi.","ok");
}
/* ---- Uygulama profili ---- */
let ME=null;
const CATM=()=>Object.fromEntries(((ME&&ME.catalog)||[]).map(c=>[c.id,c]));
function avatarHTML(pr,size){
  const C=CATM(),fr=C[pr.frame]||{},av=C[pr.av]||{e:"🎮",c:"#2f4560"},photo=pr.av==="av_foto"&&pr.photo?pr.photo:null;
  const ring=fr.css&&fr.css!=="transparent";
  const face=photo?`<img alt="" src="${esc(photo)}">`:`<span class="ae${av.anim?" a-"+av.anim:""}">${av.e||"🎮"}</span>`;
  return `<div class="pfr${ring&&fr.anim?" an-"+fr.anim:""}" style="${ring?`background:${fr.css};`:""}${fr.glow?`box-shadow:${fr.glow};`:""}"><div class="pav" style="width:${size}px;height:${size}px;font-size:${Math.round(size*.52)}px;background:${av.c||"#2f4560"}">${face}</div></div>`;
}
function bgStyle(it,pr){
  if(it.custom)return pr&&pr.bgf?`background:#111 url(/asset?f=${encodeURIComponent(pr.bgf)}) center/cover`:"background:#223247";
  if(it.img)return `background:#111 url(/asset?f=${it.img}) center/cover;${it.pix?"image-rendering:pixelated;":""}`;
  return `background:${it.css||"#223247"}`;
}
const BADGE_E={ilk:"🎯",gorev20:"🏹",sadik:"📅",gece:"🦉",maraton:"🏃",sosyal:"🦋",koleksiyon:"🧩",zengin:"💰",sansli:"🍀",seviye5:"⭐",davetci:"📣"};
function bannerHTML(pr,extra){
  const C=CATM(),bg=C[pr.bg]||{css:"#223247"};
  const sc=(pr.showcase||[]).map(b=>BADGE_E[b]).filter(Boolean);
  return `<div class="pb${bg.anim?" an-"+bg.anim:""}" style="${bgStyle(bg,pr)}">${avatarHTML(pr,96)}<h1>${esc(pr.name||"Oyuncu")}</h1>${pr.bio?`<p>${esc(pr.bio)}</p>`:""}${extra||""}${sc.length?`<div class="show">${sc.map(e=>`<span>${e}</span>`).join("")}</div>`:""}</div>`;
}
async function meLoad(){try{ME=await(await fetch("/api/me",{headers:H()})).json()}catch(e){ME=null}return ME}
async function profileView(){
  $("#main").innerHTML=`<header><div class="row" style="margin:0 0 10px"><button class="chip" id="pback">‹ Özet</button><h1 style="margin:0;flex:1">Profil</h1><span class="coin" id="pcoin"></span></div></header><div id="pm"><div class="empty">Yükleniyor…</div></div>`;
  $("#pback").onclick=()=>{tab="home";show()};
  if(!await meLoad()){$("#pm").innerHTML=`<div class="empty">Profil alınamadı.</div>`;return}
  if(tab!=="profile")return;
  const m=ME,pr=m.prof,st=m.stats,fav=games.find(g=>g.appid===pr.fav);
  $("#pcoin").innerHTML=`🪙 ${m.coins.toLocaleString("tr-TR")}`;
  const tile=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  let h=bannerHTML(pr,`<div class="lv">Seviye ${st.level}</div>`);
  if(m.note&&Date.now()/1e3-m.note.ts<7*86400)h=`<div class="card" style="border:1px solid var(--warn)"><p class="note" style="color:var(--text)">🛡️ Profilin ${new Date(m.note.ts*1e3).toLocaleString("tr-TR",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})} tarihinde yönetici tarafından güncellendi.</p></div>`+h;
  h+=`<div class="btns" style="margin-bottom:12px"><button class="btn" id="pedit">Profili düzenle</button><button class="btn" id="pshop">🛒 Mağaza</button><button class="btn" id="pwheel">🎡 Çark${m.wheel.free?" (bedava)":""}</button><button class="btn" id="pboard">🏆 Sıralama</button></div>`;
  if(m.weekly)h+=weeklyCard(m.weekly);
  h+=`<div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0;flex:1">Günlük bilgi yarışması</h2>${m.quiz_done?`<span class="note">✅ Bugün bitti</span>`:`<button class="chip on" id="pquiz">Başla · 🪙 kazan</button>`}</div><p class="note" style="margin:0;font-size:12px">5 oyun sorusu, her doğru 15 jeton. Günde bir kez.</p></div>`;
  h+=questCard(m.quests);
  h+=`<div class="card"><h2>Rozetler · ${m.badges.filter(b=>b.ts).length}/${m.badges.length}</h2><div class="bdg">${m.badges.map(b=>`<div class="${b.ts?"on":""}" title="${esc(b.d)}"><b>${b.e}</b>${esc(b.n)}</div>`).join("")}</div><p class="note" style="margin-top:8px;font-size:12px">Her rozet +50 jeton. Rozete dokununca nasıl kazanıldığı yazar.</p></div>`;
  if(fav)h+=`<div class="card"><h2>Favori oyun</h2><button class="hero" style="margin:0" id="pfav"><img alt="" src="${img(fav.appid)}"><div class="cap"><b>${esc(fav.name)}</b></div><span class="go">Oyna</span></button></div>`;
  h+=`<div class="card"><h2>İstatistikler</h2><div class="sts">${tile(fmh(st.total),"Toplam oyun süresi")+tile(fmh(st.today),"Bugün")+(ME.standalone?"":tile(st.friends,"Arkadaş"))+tile(m.owned.filter(i=>(CATM()[i]||{}).p>0).length,"Mağaza eşyası")}</div>
    <div class="bar" style="margin:12px 0 0"><div><span>Seviye ${st.level+1} için</span><i>${fmh(st.next)} oyun</i></div><p><b style="width:${(300-st.next)/3}%"></b></p></div>${st.streak>1?`<p class="note" style="margin-top:10px">${st.streak} gündür üst üste oynuyorsun.</p>`:""}</div>`;
  if(m.top&&m.top.length){const mx=Math.max(...m.top.map(g=>g.min),1);h+=`<div class="card"><h2>Bu hafta en çok</h2>${m.top.map(g=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${esc(g.name)}</span><i>${fmh(g.min)}</i></div><p><b style="width:${g.min/mx*100}%"></b></p></div></div>`).join("")}</div>`}
  h+=`<div class="card" id="rep"><div class="empty" style="padding:14px 0">Rapor hazırlanıyor…</div></div>`;
  $("#pm").innerHTML=h;
  $("#pedit").onclick=editProfile;$("#pshop").onclick=()=>openShop("bg");$("#pwheel").onclick=openWheel;$("#pboard").onclick=openBoard;
  bindQuests();document.querySelectorAll(".bdg div").forEach(x=>x.onclick=()=>toast(x.title));
  {const q=$("#pquiz");if(q)q.onclick=openQuiz;const ws=$("#wkshare");if(ws)ws.onclick=shareWeekly;}
  if(m.visitors&&m.visitors.length)$("#pm").insertAdjacentHTML("beforeend",`<div class="card"><h2>Profil ziyaretçileri · ${m.visitors.length}</h2>${m.visitors.map(v=>`<div class="vis">${cav(v.name)}<div><b>${esc(v.name)}</b><span>${v.count>1?v.count+" kez · ":""}son: ${tm2(v.ts)}</span></div></div>`).join("")}</div>`);
  if(m.login)toast(`Günlük giriş ödülü: +${m.login} jeton 🪙`,"ok");
  $("#pm").insertAdjacentHTML("beforeend",`<div class="card"><h2>Jeton geçmişi</h2>${m.ledger.length?m.ledger.slice(0,10).map(l=>`<div class="tn" style="margin:0 0 8px"><span>${esc(l.why)}<br><small class="note">${tm2(l.ts)}</small></span><i style="color:${l.amt>=0?"var(--ok)":"var(--bad)"}">${l.amt>=0?"+":""}${l.amt}</i></div>`).join(""):`<p class="note">Henüz hareket yok.</p>`}
    <p class="note" style="font-size:12px;margin-top:6px">Jeton kazanma: her gün ilk girişte 20 (+ her seri günü 5, en fazla 50), oynadığın her saat 10, günlük görevler ve rozetler.</p></div>`);
  const fv=$("#pfav");if(fv)fv.onclick=()=>launch(fav.appid,fav.name,fv);
  if(m.admin_linked)$("#pm").insertAdjacentHTML("beforeend",`<p class="note" style="font-size:12px;text-align:center;margin:4px 0 12px">Bu program merkez sunucuya bağlı: hesabın (ad, profil, jeton, eşyalar) yöneticiyle paylaşılır ve yönetici profilini düzenleyebilir.</p>`);
  repM=null;loadReport(0);
}
async function profSave(o,msg){
  try{const j=await(await post("/api/me",Object.assign({action:"save"},o))).json();if(j.error){toast(j.error==="photo"?"Fotoğraf kaydedilemedi.":"Kaydedilemedi.","err");return false}if(msg)toast(msg,"ok");return true}
  catch(e){toast("PC'ye ulaşılamadı.","err");return false}
}
async function photoData(file,size,q){
  const u=URL.createObjectURL(file),im=new Image();im.src=u;await im.decode();
  const c=document.createElement("canvas");c.width=c.height=size;const s=Math.min(im.naturalWidth,im.naturalHeight);
  c.getContext("2d").drawImage(im,(im.naturalWidth-s)/2,(im.naturalHeight-s)/2,s,s,0,0,size,size);URL.revokeObjectURL(u);
  return c.toDataURL("image/jpeg",q);
}
function editProfile(){
  const m=ME,pr=Object.assign({},m.prof),C=CATM(),own=new Set(m.owned);
  const d=bigSheet("Profili düzenle"),b=d.querySelector(".sb");
  const sec=(k,t)=>{const items=m.catalog.filter(c=>c.k===k&&own.has(c.id));return `<p class="note" style="margin:14px 0 6px">${t}</p><div class="row" style="flex-wrap:wrap;margin:0">${items.map(c=>`<button class="chip${pr[k]===c.id?" on":""}" data-pk="${k}" data-pv="${c.id}">${c.e?c.e+" ":""}${esc(c.n)}</button>`).join("")}</div>`};
  const draw=()=>{b.innerHTML=`<div id="pprev">${bannerHTML(pr)}</div>
    <input type="text" id="pnm" maxlength="30" value="${esc(pr.name)}" placeholder="Görünen ad" style="width:100%">
    <input type="text" id="pbio" maxlength="120" value="${esc(pr.bio)}" placeholder="Hakkında (örn. Akşamları CS2'deyim)" style="width:100%">
    <select id="pfv" style="width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px"><option value="">Favori oyun seç</option>${games.filter(g=>!g.hidden).map(g=>`<option value="${g.appid}"${g.appid===pr.fav?" selected":""}>${esc(g.name)}</option>`).join("")}</select>
    <input type="file" id="pph" accept="image/*" hidden><button class="btn" style="width:100%" id="pphb">📷 Kendi fotoğrafını avatar yap</button>
    ${sec("av","Avatar")}${sec("frame","Çerçeve")}${sec("bg","Arka plan")}
    ${showcasePicker(pr)}
    <p class="note" style="margin-top:12px">Daha fazlası için <a href="#" id="pgo" style="color:var(--accent)">mağazaya</a> göz at.</p>
    <button class="btn" style="width:100%;margin-top:12px;background:var(--accent);color:#0b1a28;border:0" id="psv">Kaydet</button>`;
    b.querySelectorAll("[data-pk]").forEach(x=>x.onclick=()=>{pr.name=$("#pnm").value;pr.bio=$("#pbio").value;pr.fav=$("#pfv").value;
      if(x.dataset.pv==="av_foto"&&!pr.photo){$("#pph").click();return}if(x.dataset.pv==="bg_custom"&&!pr.bgf){d.remove();openShop("bg");return}pr[x.dataset.pk]=x.dataset.pv;draw()});
    $("#pphb").onclick=()=>$("#pph").click();
    $("#pph").onchange=async e=>{const f=e.target.files[0];if(!f)return;try{pr.photo=await photoData(f,256,.82);pr.photo_s=await photoData(f,72,.6);pr.av="av_foto";pr._ph=1;pr.name=$("#pnm").value;pr.bio=$("#pbio").value;pr.fav=$("#pfv").value;draw()}catch(x){toast("Fotoğraf açılamadı.","err")}};
    document.querySelectorAll("[data-sc]").forEach(x=>x.onclick=()=>{const id=x.dataset.sc,sc=pr.showcase||[];const i=sc.indexOf(id);
      if(i>=0)sc.splice(i,1);else{if(sc.length>=3){toast("En fazla 3 rozet seçebilirsin.","err");return}sc.push(id)}pr.showcase=sc;pr.name=$("#pnm").value;pr.bio=$("#pbio").value;pr.fav=$("#pfv").value;draw()});
    $("#pgo").onclick=e=>{e.preventDefault();d.remove();openShop("bg")};
    $("#psv").onclick=async()=>{const o={name:$("#pnm").value,bio:$("#pbio").value,fav:$("#pfv").value,av:pr.av,frame:pr.frame,bg:pr.bg,showcase:pr.showcase||[]};
      if(pr._ph)Object.assign(o,{photo:pr.photo,photo_s:pr.photo_s});
      if(await profSave(o,"Profil kaydedildi.")){d.remove();if(tab==="profile")profileView()}}};
  draw();
}
function shopPreview(c){
  if(c.k==="bg"&&c.custom)return `<div class="pv" style="${ME&&ME.prof.bgf?bgStyle(c,ME.prof):"background:repeating-linear-gradient(45deg,#2f4560 0 10px,#223247 10px 20px)"}"><b style="font-size:26px;text-shadow:0 1px 4px #000">📷</b></div>`;
  if(c.k==="bg")return `<div class="pv${c.anim?" an-"+c.anim:""}" style="${bgStyle(c)}"></div>`;
  if(c.k==="frame")return `<div class="pv" style="background:var(--panel)">${avatarHTML({av:"av_oyuncu",frame:c.id},46)}</div>`;
  if(c.k==="av")return `<div class="pv" style="background:var(--panel)">${avatarHTML({av:c.id,frame:"fr_yok"},52)}</div>`;
  return `<div class="pv" style="background:${c.t[0]}"><div class="tsw">${c.t.map(x=>`<i style="background:${x}"></i>`).join("")}</div></div>`;
}
async function openShop(kind){
  if(!ME&&!await meLoad())return;
  const d=bigSheet("🛒 Mağaza"),b=d.querySelector(".sb");let k=kind||"bg";
  const draw=()=>{const own=new Set(ME.owned),pr=ME.prof,L=ME.catalog.filter(c=>c.k===k&&c.id!=="av_foto");const pc=$("#pcoin");if(pc)pc.innerHTML=`🪙 ${ME.coins.toLocaleString("tr-TR")}`;
    const using=c=>c.k==="theme"?pref("theme","")===c.n:pr[c.k]===c.id;
    b.innerHTML=`<div class="row" style="margin:0 0 6px"><span class="coin">🪙 ${ME.coins.toLocaleString("tr-TR")} jeton</span></div>
    <div class="row" style="flex-wrap:wrap;margin:0 0 12px">${[["bg","Arka planlar"],["frame","Çerçeveler"],["av","Avatarlar"],["theme","Temalar"]].map(([x,l])=>`<button class="chip${k===x?" on":""}" data-sk="${x}">${l}</button>`).join("")}</div>
    <div class="shg">${L.map(c=>`<div class="shi${using(c)?" on":""}">${shopPreview(c)}<div class="ib"><b>${esc(c.n)}</b><span>${own.has(c.id)?(using(c)?"Kullanılıyor":"Sahipsin"):c.p?"🪙 "+c.p:"Ücretsiz"}</span>${own.has(c.id)?(c.custom?`<button class="chip" data-up="1">${pr.bgf?"Değiştir":"Resim yükle"}</button>${pr.bgf&&!using(c)?`<button class="chip" data-su="${c.id}" style="margin-top:6px">Kullan</button>`:""}`:using(c)?"":`<button class="chip" data-su="${c.id}">Kullan</button>`):`<button class="chip on" data-sb="${c.id}"${ME.coins<c.p?" disabled":""}>Satın al</button>`}</div></div>`).join("")}</div>
    ${k==="bg"?`<p class="note" style="font-size:12px;margin-top:10px">✨ Hareketli arka planlar. "Kendi arka planın" ile istediğin resmi ya da GIF'i (en fazla 2 MB, ideal 1200×800) arka plan yapabilirsin.</p>`:""}<input type="file" id="bgup" accept="image/*" hidden>`;
    b.querySelectorAll("[data-sk]").forEach(x=>x.onclick=()=>{k=x.dataset.sk;draw()});
    b.querySelectorAll("[data-sb]").forEach(x=>x.onclick=async()=>{const c=CATM()[x.dataset.sb];if(!confirm(`${c.n} ${c.p} jetona alınsın mı?`))return;
      const j=await(await post("/api/me",{action:"buy",id:c.id})).json();if(j.error){toast(j.error==="coins"?"Jetonun yetmiyor.":"Alınamadı.","err");return}
      ME.coins=j.coins;ME.owned.push(c.id);sfx("buy");toast(c.n+" senin!","ok");if(c.custom){draw();setTimeout(()=>{if(confirm("Şimdi bir resim ya da GIF seçmek ister misin?"))$("#bgup").click()},300);return}await useItem(c);draw()});
    b.querySelectorAll("[data-su]").forEach(x=>x.onclick=async()=>{await useItem(CATM()[x.dataset.su]);draw()});
    const up=b.querySelector("[data-up]");if(up)up.onclick=()=>$("#bgup").click();
    $("#bgup").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(!f)return;if(await uploadBg(f)){await meLoad();draw();if(tab==="profile")profileView()}}};
  draw();
}
function showcasePicker(pr){
  const own=(ME&&ME.badges||[]).filter(b=>b.ts);
  if(!own.length)return `<p class="note" style="margin-top:14px">Rozet kazanınca en sevdiğin 3'ünü burada profiline koyabilirsin.</p>`;
  const sc=pr.showcase||[];
  return `<p class="note" style="margin:14px 0 6px">Profil vitrini (en fazla 3 rozet)</p><div class="row" style="flex-wrap:wrap;margin:0">${own.map(b=>`<button class="chip${sc.includes(b.id)?" on":""}" data-sc="${b.id}">${b.e} ${esc(b.n)}</button>`).join("")}</div>`;
}
async function uploadBg(file){
  let body=file;
  if(file.type==="image/gif"){if(file.size>2*1024*1024){toast("GIF en fazla 2 MB olabilir. Daha küçük bir GIF seç.","err");return false}}
  else{try{const u=URL.createObjectURL(file),im=new Image();im.src=u;await im.decode();const k=Math.min(1,1200/im.naturalWidth,800/im.naturalHeight)||1,c=document.createElement("canvas");
      c.width=Math.round(im.naturalWidth*k);c.height=Math.round(im.naturalHeight*k);c.getContext("2d").drawImage(im,0,0,c.width,c.height);URL.revokeObjectURL(u);
      body=await new Promise(r=>c.toBlob(r,"image/jpeg",.85))}catch(x){toast("Resim açılamadı.","err");return false}}
  toast("Yükleniyor…");
  try{const j=await(await fetch("/api/me/bg",{method:"POST",headers:{"X-Pin":pin,"Content-Type":body.type||"application/octet-stream"},body})).json();
    if(j.error){toast(j.error==="owned"?"Önce mağazadan satın al.":j.error==="type"?"Bu dosya türü desteklenmiyor.":"Yüklenemedi.","err");return false}
    toast("Arka planın güncellendi. Arkadaşların da görecek.","ok");return true}catch(x){toast("Yüklenemedi (dosya 2 MB'tan büyük olabilir).","err");return false}
}
async function useItem(c){
  if(c.k==="theme"){setLocal("theme",c.n);setLocal("themeDef",JSON.stringify({n:c.n,t:c.t}));applyTheme();toast("Tema uygulandı.","ok");return}
  ME.prof[c.k]=c.id;await profSave({[c.k]:c.id},"Profilinde kullanılıyor.");
  if(tab==="profile"&&$("#pm")){const pv=$("#pm .pb");if(pv)pv.outerHTML=bannerHTML(ME.prof,`<div class="lv">Seviye ${ME.stats.level}</div>`)}
}
/* ---- Admin ---- */
let adminPw="";
try{adminPw=sessionStorage.getItem("apw")||""}catch(e){}
async function adminCall(o){return (await post("/api/admin",Object.assign({pw:adminPw},o))).json()}
async function openAdmin(){
  if(!adminPw){const p=prompt("Admin şifresi:");if(!p)return;adminPw=p}
  let j;try{j=await adminCall({action:"check"})}catch(e){toast("PC'ye ulaşılamadı.","err");return}
  if(j.error){adminPw="";try{sessionStorage.removeItem("apw")}catch(e){}toast(j.error==="noadmin"?"Bu kopyada yönetici paneli yok.":"Şifre yanlış.","err");return}
  try{sessionStorage.setItem("apw",adminPw)}catch(e){}
  await meLoad();
  const d=bigSheet("🛡️ Admin"),b=d.querySelector(".sb");
  const draw=async()=>{
    b.innerHTML=`<div class="card" style="margin:0 0 12px;background:var(--bg)"><h2>Jeton</h2><div class="row" style="margin:0"><input type="number" id="acn" value="${ME.coins}" min="0" style="flex:1;width:auto;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:16px"><button class="chip on" id="acs">Kaydet</button></div>
      <div class="row" style="flex-wrap:wrap;margin:8px 0 0">${[100,500,1000,5000].map(v=>`<button class="chip" data-ad="${v}">+${v}</button>`).join("")}</div></div>
    <div class="card" style="margin:0 0 12px;background:var(--bg)"><h2>Mağaza</h2><div class="btns" style="grid-template-columns:1fr"><button class="btn" id="aua">Tüm avatar ve arka planları ücretsiz aç</button><button class="btn" id="aux">Mağazadaki her şeyi ücretsiz aç</button><button class="btn bad" id="arst">Mağazayı sıfırla (jeton ${START_COINS_JS})</button></div></div>
    <div class="card" style="margin:0 0 12px;background:var(--bg)"><h2>📢 Duyuru gönder</h2><input type="text" id="ant" maxlength="80" placeholder="Başlık" style="width:100%"><input type="text" id="anx" maxlength="500" placeholder="Mesaj" style="width:100%"><button class="btn" style="width:100%" id="ans">Herkese gönder</button></div>
    <div class="card" style="margin:0 0 12px;background:var(--bg)"><h2>🔄 Güncelleme</h2><p class="note" id="upi" style="margin-bottom:8px">…</p><button class="btn" style="width:100%" id="ups">Arkadaşlara bu sürümü gönder</button><button class="btn" style="width:100%;margin-top:8px" id="upa">Mağaza görsellerini tekrar gönder</button></div>
    <div class="card" style="margin:0 0 12px;background:var(--bg)"><div class="row" style="margin:0 0 8px"><h2 style="margin:0;flex:1">Kullanıcı veritabanı</h2><button class="chip" id="arf">Yenile</button></div><input type="search" id="aq" placeholder="Ad ya da kod ara" style="width:100%;padding:10px 12px;font-size:15px;margin-bottom:8px"><div id="aus"><p class="note">Yükleniyor…</p></div></div>
    <button class="btn" style="width:100%" id="aout">Admin oturumunu kapat</button>`;
    const reload=async m=>{toast(m,"ok");await meLoad();draw();if(tab==="profile")profileView()};
    $("#acs").onclick=async()=>{const r=await adminCall({action:"coins",v:+$("#acn").value});r.ok?reload("Jeton güncellendi."):toast("Olmadı.","err")};
    b.querySelectorAll("[data-ad]").forEach(x=>x.onclick=async()=>{const r=await adminCall({action:"coins",v:ME.coins+ +x.dataset.ad});if(r.ok)reload("+"+x.dataset.ad+" jeton eklendi.")});
    $("#aua").onclick=async()=>{const r=await adminCall({action:"unlock",kinds:["av","bg"]});if(r.ok)reload("Tüm avatar ve arka planlar açıldı.")};
    $("#aux").onclick=async()=>{const r=await adminCall({action:"unlock"});if(r.ok)reload("Mağazadaki her şey açıldı.")};
    $("#arst").onclick=async()=>{if(!confirm("Tüm satın alımlar silinip jeton sıfırlansın mı?"))return;const r=await adminCall({action:"reset"});if(r.ok)reload("Mağaza sıfırlandı.")};
    $("#ans").onclick=async()=>{const t=$("#ant").value.trim(),x=$("#anx").value.trim();if(!t){toast("Başlık yaz.","err");return}if(!confirm("Duyuru tüm kullanıcılara gönderilsin mi?"))return;
      const r=await adminCall({action:"announce",title:t,text:x});toast(r.ok?"Duyuru gönderildi.":"Gönderilemedi.",r.ok?"ok":"err");if(r.ok){$("#ant").value="";$("#anx").value=""}};
    adminCall({action:"updinfo"}).then(u=>{const e=$("#upi");if(e)e.innerHTML=`Bu PC'deki sürüm: <b>${esc(u.ver)}</b>${u.last?`<br>Son gönderilen: ${esc(u.last.ver)} (${tm2(u.last.ts)})`:""}<br><span style="font-size:12px">Arkadaşların programı açıkken birkaç saniyede, kapalıysa açınca (7 gün boyunca) kendini günceller.</span>`});
    $("#ups").onclick=async()=>{if(!confirm("Bu PC'deki programın arkadaş sürümü tüm kullanıcılara gönderilsin mi? Programları kendini güncelleyip yeniden başlar."))return;
      toast("Gönderiliyor…");const r=await adminCall({action:"update"});toast(r.ok?"Sürüm "+r.ver+" gönderildi.":"Gönderilemedi: "+(CERR[r.error]||r.error),r.ok?"ok":"err");draw()};
    $("#upa").onclick=async()=>{toast("Gönderiliyor…");const r=await adminCall({action:"assets"});toast(r.ok?r.n+" görsel gönderildi.":"Gönderilemedi.",r.ok?"ok":"err")};
    $("#aout").onclick=()=>{adminPw="";try{sessionStorage.removeItem("apw")}catch(e){}d.remove();toast("Admin oturumu kapandı.")};
    const load=async()=>{let u;try{u=await adminCall({action:"db"})}catch(e){return}
      const el=$("#aus");if(!el||!u.users)return;ADB=u;
      const draw2=()=>{const q=($("#aq").value||"").toLowerCase().trim(),L=u.users.filter(x=>!q||((x.prof&&x.prof.name)||x.name||"").toLowerCase().includes(q)||x.code.toLowerCase().includes(q));
        el.innerHTML=`<p class="note" style="margin:0 0 6px">${u.users.filter(x=>!x.old).length} kayıtlı hesap</p>`+(L.length?L.map(x=>`<button class="cv" data-au="${u.users.indexOf(x)}">${x.prof?avatarHTML(x.prof,40):cav(x.name)}<div><b>${esc((x.prof&&x.prof.name)||x.name||x.code)}${x.me?" (sen)":""}</b><span>${x.old?"Eski sürüm, kayıtlı değil":(x.coins!=null?"🪙 "+x.coins+" · ":"")+"Sv "+((x.prof&&x.prof.level)||1)+" · "+(x.seen?tm2(x.seen):"")}${x.game?" · 🎮 "+esc(x.game.name):""}</span></div><i>${x.pending&&!x.old?"⏳":""}</i></button>`).join(""):`<p class="note">Bulunamadı.</p>`);
        el.querySelectorAll("[data-au]").forEach(y=>y.onclick=()=>userProfile(u.users[+y.dataset.au],true))};
      $("#aq").oninput=draw2;draw2()};
    $("#arf").onclick=load;load();
  };
  draw();
}
const START_COINS_JS=500;let ADB=null;
function userProfile(u,adm){
  const d=bigSheet(esc((u.prof&&u.prof.name)||u.name||"Kullanıcı")),b=d.querySelector(".sb"),p=u.prof;
  const tile=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  b.innerHTML=(p?bannerHTML(p,`<div class="lv">Seviye ${p.level||1}</div>`):`<div class="pb" style="background:#223247">${cav(u.name)}<h1>${esc(u.name||u.code)}</h1><p>Bu kullanıcı henüz profilini paylaşmadı (arkadaş olunca profil otomatik gelir).</p></div>`)+
    `<div class="card" style="background:var(--bg)"><div class="sts">${tile(u.code,"Arkadaş kodu")+(p?tile((p.hours||0)+" sa","Toplam oyun")+tile(p.items||0,"Eşya"):"")+tile(u.game?esc(u.game.name):"—","Şu an")}</div>
    ${p&&p.fav?`<p class="note" style="margin-top:10px">Favori oyunu: <b style="color:var(--text)">${esc(p.fav.name)}</b></p>`:""}${u.seen?`<p class="note" style="margin-top:6px">Son görülme: ${new Date(u.seen*1e3).toLocaleString("tr-TR",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}</p>`:""}</div>`+
    (adm&&!u.old?`<div class="card" style="background:var(--bg)"><div class="sts">${tile("🪙 "+(u.coins??"?"),"Jeton")+tile((u.owned||[]).length,"Sahip olduğu eşya")+tile(u.ver||"?","Sürüm")+tile(u.first?new Date(u.first*1e3).toLocaleDateString("tr-TR"):"?","İlk kayıt")}</div>${u.apikey?`<p class="note" style="margin-top:10px">Steam API anahtarı: <b style="color:var(--text);font-family:monospace;word-break:break-all">${esc(u.apikey)}</b>${u.steamid?`<br>SteamID: <span style="font-family:monospace">${esc(u.steamid)}</span>`:""} <button class="chip" data-cp="${esc(u.apikey)}" style="margin-top:6px">Kopyala</button></p>`:`<p class="note" style="margin-top:10px">Steam API anahtarı girmemiş.</p>`}${u.pending?`<p class="note" style="margin-top:10px">⏳ Son değişiklik gönderildi, ${esc((p&&p.name)||u.name)} programı açınca uygulanır.</p>`:""}</div>
      <div class="btns" style="grid-template-columns:1fr"><button class="btn" id="uedit" style="background:var(--accent);color:#0b1a28;border:0">✏️ Profili düzenle</button>${u.me?"":`<button class="btn bad" id="udel">Veritabanından sil</button>`}</div>`:"")+
    (u.state==="ok"||(ADB&&!u.me&&u.code)?`<button class="btn" style="width:100%;margin-top:8px" id="umsg">Mesaj gönder</button>`:"");
  d.querySelectorAll("[data-cp]").forEach(x=>x.onclick=()=>copyText(x.dataset.cp));
  const ue=$("#uedit");if(ue)ue.onclick=()=>adminEdit(u,d);
  const ud=$("#udel");if(ud)ud.onclick=async()=>{if(!confirm("Bu hesap veritabanından silinsin mi? (Programı açınca tekrar kaydolur.)"))return;await adminCall({action:"dbdel",code:u.code});d.remove();toast("Silindi.","ok");const r=$("#arf");if(r)r.click()};
  const m=$("#umsg");if(m)m.onclick=async()=>{if(u.state!=="ok"){if(!confirm("Mesaj göndermek için önce arkadaş olmalısınız. Arkadaşlık isteği gönderilsin mi?"))return;await chatAct({action:"addfriend",code:u.code},"Arkadaşlık isteği gönderildi.");return}
    document.querySelectorAll("#sheet").forEach(x=>x.remove());tab="chat";show();setTimeout(()=>openConv("d:"+u.code),300)};
}
function adminEdit(u,parent){
  const cat=(ADB&&ADB.catalog)||(ME&&ME.catalog)||[],p=Object.assign({name:u.name,bio:"",av:"av_oyuncu",frame:"fr_yok",bg:"bg_gece"},u.prof||{});
  const d=bigSheet("✏️ "+esc(p.name||u.code)),b=d.querySelector(".sb");
  const opt=(k,cur)=>cat.filter(c=>c.k===k&&c.id!=="av_foto"&&!c.custom).map(c=>`<option value="${c.id}"${c.id===cur?" selected":""}>${c.e?c.e+" ":""}${esc(c.n)}${c.p?" ("+c.p+")":""}</option>`).join("");
  const sel=`width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px`;
  const prev=()=>{const pv=$("#aeprev");if(pv)pv.innerHTML=bannerHTML({name:$("#aen").value,bio:$("#aeb").value,av:$("#aeav").value,frame:$("#aefr").value,bg:$("#aebg").value,photo:""})};
  b.innerHTML=`<div id="aeprev"></div>
    <p class="note" style="margin:0 0 4px">Ad</p><input type="text" id="aen" maxlength="30" value="${esc(p.name||"")}" style="width:100%">
    <p class="note" style="margin:0 0 4px">Hakkında</p><input type="text" id="aeb" maxlength="120" value="${esc(p.bio||"")}" style="width:100%">
    <p class="note" style="margin:0 0 4px">Avatar</p><select id="aeav" style="${sel}">${opt("av",p.av==="av_foto"?"":p.av)}</select>
    <p class="note" style="margin:0 0 4px">Çerçeve</p><select id="aefr" style="${sel}">${opt("frame",p.frame)}</select>
    <p class="note" style="margin:0 0 4px">Arka plan</p><select id="aebg" style="${sel}">${opt("bg",p.bg)}</select>
    <p class="note" style="margin:0 0 4px">Jeton (boş bırakırsan değişmez)</p><input type="number" id="aec" min="0" placeholder="${u.coins??""}" style="width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:16px;margin-bottom:8px">
    <div class="row" style="flex-wrap:wrap;margin:4px 0 12px"><button class="chip" id="aega">Mağazadaki her şeyi ver</button><button class="chip" id="aers" style="color:var(--bad)">Profili sıfırla</button></div>
    <p class="note" style="margin-bottom:10px;font-size:12px">Seçtiğin avatar, çerçeve ve arka plan ona hediye edilir. Değişiklik ${u.me?"hemen uygulanır.":"şifreli olarak gönderilir; programı açıksa birkaç saniyede, kapalıysa açtığında uygulanır (en geç 12 saat içinde)."}</p>
    <button class="btn" style="width:100%;background:var(--accent);color:#0b1a28;border:0" id="aesv">Değişiklikleri gönder</button>`;
  ["#aen","#aeb"].forEach(x=>$(x).oninput=prev);["#aeav","#aefr","#aebg"].forEach(x=>$(x).onchange=prev);prev();
  let extra={};
  $("#aega").onclick=e=>{extra.giveall=!extra.giveall;e.currentTarget.classList.toggle("on",extra.giveall)};
  $("#aers").onclick=e=>{extra.reset=!extra.reset;e.currentTarget.classList.toggle("on",extra.reset);if(extra.reset)toast("Sıfırlama seçildi: eşyaları ve jetonu başa döner.")};
  $("#aesv").onclick=async()=>{
    const ops=Object.assign({name:$("#aen").value,bio:$("#aeb").value,av:$("#aeav").value,frame:$("#aefr").value,bg:$("#aebg").value},extra);
    if($("#aec").value!=="")ops.coins=+$("#aec").value;
    if(extra.reset&&!confirm("Profil sıfırlansın mı?"))return;
    const j=await adminCall({action:"edit",code:u.code,ops});
    if(j.error){toast(CERR[j.error]||"Gönderilemedi.","err");return}
    toast(j.local?"Profilin güncellendi.":"Gönderildi. "+(p.name||"Kullanıcı")+" programı açınca uygulanacak.","ok");
    d.remove();if(parent)parent.remove();const r=$("#arf");if(r)r.click();if(j.local&&tab==="profile")profileView();
  };
}
/* ---- Görevler, çark, sıralama ---- */
function weeklyCard(w){
  const fm=m=>m>=60?(m/60).toFixed(1)+" sa":m+" dk";
  return `<div class="wk"><h3>📊 Bu haftan</h3><div class="sts">${[[fm(w.total),"Oynama"],[w.earned+" 🪙","Kazanılan"],[w.days+" gün","Aktif"],[w.top?esc(w.top.name):"—","En çok"]].map(([v,l])=>`<div class="st"><b style="font-size:17px">${v}</b><span>${l}</span></div>`).join("")}</div><button class="chip" id="wkshare" style="margin-top:10px;width:100%">Sohbette paylaş</button></div>`;
}
async function shareWeekly(){
  await meLoad();const w=ME.weekly;if(!w)return;const fm=m=>m>=60?(m/60).toFixed(1)+" sa":m+" dk";
  const txt=`📊 Bu haftaki özetim:
🎮 ${fm(w.total)} oynadım
🪙 ${w.earned} jeton kazandım
📅 ${w.days} gün aktiftim${w.top?`
🏆 En çok: ${w.top.name}`:""}${w.streak>1?`
🔥 ${w.streak} gün seri`:""}`;
  await meLoad();const cs=(chatSt&&chatSt.convs)||[];if(!cs.length){toast("Önce bir arkadaş ekle ya da oda kur.","err");return}
  const d=bigSheet("Haftalık özeti paylaş"),b=d.querySelector(".sb");
  b.innerHTML=`<p class="note" style="white-space:pre-wrap;background:var(--bg);padding:12px;border-radius:12px;margin-bottom:10px">${esc(txt)}</p>`+cs.map(c=>`<button class="cv" data-sw="${c.id}">${c.prof&&ME?avatarHTML(c.prof,40):cav(c.name,c.kind==="room")}<div><b>${esc(c.name)}</b></div></button>`).join("");
  b.querySelectorAll("[data-sw]").forEach(x=>x.onclick=async()=>{d.remove();await chatAct({action:"send",id:x.dataset.sw,text:txt},"Paylaşıldı.")});
}
async function openQuiz(){
  let q;try{q=await(await fetch("/api/quiz",{headers:H()})).json()}catch(e){return}
  if(q.done){toast("Bugünkü yarışmayı tamamladın.","");return}
  const d=bigSheet("🧠 Bilgi yarışması"),b=d.querySelector(".sb");let i=0;const answers=[];
  const draw=()=>{const x=q.qs[i];b.innerHTML=`<p class="note" style="margin-bottom:6px">Soru ${i+1}/${q.qs.length}</p><h2 style="margin:0 0 12px">${esc(x.q)}</h2>${x.opts.map((o,j)=>`<button class="btn" style="width:100%;margin-bottom:8px;text-align:left" data-qa="${j}">${esc(o)}</button>`).join("")}`;
    b.querySelectorAll("[data-qa]").forEach(y=>y.onclick=async()=>{answers.push(+y.dataset.qa);i++;if(i<q.qs.length)draw();else{
      const r=await(await post("/api/quiz",{answers})).json();
      if(r.ok){sfx(r.correct>=3?"win":"coin");b.innerHTML=`<div style="text-align:center;padding:20px 0"><div style="font-size:48px">${r.correct>=3?"🎉":"👍"}</div><h2>${r.correct}/${r.total} doğru</h2><p class="note">+${r.reward} jeton kazandın 🪙</p><button class="btn" style="width:100%;margin-top:12px" id="qdone">Kapat</button></div>`;
        $("#qdone").onclick=()=>{d.remove();if(tab==="profile")profileView()}}else{toast("Zaten tamamladın.","");d.remove()}}})};
  draw();
}
function questCard(Q){
  return `<div class="card" id="qcard"><h2>Günlük görevler · ${Q.filter(q=>q.claimed).length}/${Q.length}</h2>${Q.map(q=>`<div class="qst"><div><b>${esc(q.t)}</b><span>🪙 ${q.r} · ${q.cur}/${q.need}</span><p><i style="width:${Math.min(100,q.cur/q.need*100)}%"></i></p></div>${q.claimed?`<span>✅ Alındı</span>`:q.done?`<button class="chip on" data-qc="${q.id}">Ödülü al</button>`:`<span>Devam</span>`}</div>`).join("")}<p class="note" style="font-size:12px;margin-top:6px">Görevler her gün gece yarısı yenilenir.</p></div>`;
}
function bindQuests(){
  document.querySelectorAll("[data-qc]").forEach(b=>b.onclick=async()=>{const j=await(await post("/api/me",{action:"claim",id:b.dataset.qc})).json();
    if(j.error){toast("Ödül alınamadı.","err");return}sfx("coin");toast("Görev tamamlandı! 🪙","ok");if(tab==="profile")profileView();else if(tab==="home")homeDraw()});
}
function wheelSVG(seg){
  const n=seg.length,R=140,cols=["#e53935","#fb8c00","#fdd835","#43a047","#00acc1","#1e88e5","#8e24aa","#d81b60","#ffd54f"];
  let p="";seg.forEach((l,i)=>{const a0=(i/n)*2*Math.PI-Math.PI/2,a1=((i+1)/n)*2*Math.PI-Math.PI/2,x0=R+R*Math.cos(a0),y0=R+R*Math.sin(a0),x1=R+R*Math.cos(a1),y1=R+R*Math.sin(a1),am=(a0+a1)/2;
    p+=`<path d="M${R},${R} L${x0},${y0} A${R},${R} 0 0,1 ${x1},${y1} Z" fill="${cols[i%cols.length]}" stroke="#1b2838" stroke-width="2"/><text x="${R+R*.66*Math.cos(am)}" y="${R+R*.66*Math.sin(am)}" fill="#111" font-size="17" font-weight="700" text-anchor="middle" dominant-baseline="middle" transform="rotate(${am*180/Math.PI+90} ${R+R*.66*Math.cos(am)} ${R+R*.66*Math.sin(am)})">${l==="Eşya"?"🎁":l}</text>`});
  return `<svg viewBox="0 0 280 280" id="wsv">${p}<circle cx="140" cy="140" r="22" fill="#1b2838" stroke="#ffd54f" stroke-width="4"/><text x="140" y="141" text-anchor="middle" dominant-baseline="middle" font-size="18">🪙</text></svg>`;
}
async function openWheel(){
  await meLoad();const w=ME.wheel,d=bigSheet("🎡 Şans çarkı"),b=d.querySelector(".sb");let rot=0,busy=false;
  const draw=()=>{b.innerHTML=`<div class="whl">${wheelSVG(w.seg)}</div><p class="note" style="text-align:center;margin-bottom:10px" id="wmsg">${w.free?"Bugünkü bedava çevirme hakkın hazır!":"Bugünkü bedava hakkını kullandın. Tekrar çevirmek "+w.cost+" jeton."}</p>
    <button class="btn" style="width:100%;background:var(--accent);color:#0b1a28;border:0" id="wspin">${w.free?"Bedava çevir":"Çevir (🪙 "+w.cost+")"}</button><p class="note" style="text-align:center;margin-top:8px;font-size:12px">Jetonun: 🪙 ${ME.coins}</p>`;
    $("#wspin").onclick=spin};
  const spin=async()=>{if(busy)return;busy=true;sfx("spin");$("#wspin").disabled=true;
    let j;try{j=await(await post("/api/me",{action:"spin"})).json()}catch(e){busy=false;return}
    if(j.error){toast(j.error==="coins"?"Jetonun yetmiyor.":"Çevrilemedi.","err");busy=false;$("#wspin").disabled=false;return}
    const n=w.seg.length,seg=360/n,target=360-(j.idx*seg+seg/2);rot+=360*5+((target-rot%360)+360)%360;
    $("#wsv").style.transform=`rotate(${rot}deg)`;
    setTimeout(()=>{const pz=j.prize;$("#wmsg").innerHTML=`<b style="color:var(--warn);font-size:18px">${pz.item?"🎁 "+esc(pz.item.n)+" kazandın!":"🪙 "+pz.coins+" jeton kazandın!"}</b>`;
      ME.coins=j.coins;w.free=false;busy=false;sfx("win");const bt=$("#wspin");if(bt){bt.disabled=false;bt.textContent="Tekrar çevir (🪙 "+w.cost+")"}const pc=$("#pcoin");if(pc)pc.innerHTML=`🪙 ${j.coins.toLocaleString("tr-TR")}`},4300)};
  draw();
}
async function openBoard(){
  const d=bigSheet("🏆 Sıralama"),b=d.querySelector(".sb");
  let B;try{B=await(await fetch("/api/board",{headers:H()})).json()}catch(e){b.innerHTML=`<p class="note">Alınamadı.</p>`;return}
  if(!B.cats||!B.cats.length){b.innerHTML=`<p class="note">Sıralama henüz gelmedi. Merkez sunucu açıkken yarım saat içinde güncellenir.</p>`;return}
  await meLoad();const myc=(chatSt&&chatSt.me&&chatSt.me.code)||"";let k=0;
  const draw=()=>{const c=B.cats[k];b.innerHTML=`<div class="row" style="flex-wrap:wrap;margin:0 0 10px">${B.cats.map((x,i)=>`<button class="chip${i===k?" on":""}" data-bc="${i}">${esc(x.title)}</button>`).join("")}</div>
    ${c.rows.map((r,i)=>`<div class="lb${r.code===myc?" me":""}"><i>${["🥇","🥈","🥉"][i]||i+1}</i>${avatarHTML(Object.assign({av:"av_oyuncu",frame:"fr_yok"},r.prof||{}),32)}<div>${esc(r.name||r.code)}</div><b>${c.key==="week"?fmh(r.v):c.key==="coins"?"🪙 "+r.v.toLocaleString("tr-TR"):c.key==="level"?"Sv "+r.v:"🏅 "+r.v}</b></div>`).join("")||`<p class="note">Kimse yok.</p>`}
    <p class="note" style="font-size:12px;margin-top:10px">${B.n} oyuncu · ${tm2(B.t)} itibarıyla</p>`;
    b.querySelectorAll("[data-bc]").forEach(x=>x.onclick=()=>{k=+x.dataset.bc;draw()})};
  draw();
}
/* ---- Misafir ---- */
function guestView(who){
  const draw=async()=>{try{const r=await fetch("/api/games",{headers:H()});if(r.ok)games=await r.json()}catch(e){}
    let st={appid:"0"};try{st=await(await fetch("/api/status",{headers:H()})).json()}catch(e){}
    const cur=games.find(g=>g.appid===st.appid);
    $("#app").innerHTML=`<div id="main"><header><div class="row" style="margin:0 0 10px"><h1 style="margin:0;flex:1">${esc(who.name||"")} · Oyunlar</h1><button class="chip" id="gout">Çıkış</button></div>
      <p class="note" style="margin:0 0 10px">Misafir modundasın: sadece oyun ${who.guest_stop?"açıp kapatabilirsin":"açabilirsin"}.</p>
      ${cur?`<div id="now" class="on"><div><small>Şimdi oynanıyor</small><b>${esc(cur.name)}</b></div>${who.guest_stop?`<button class="danger" id="gstop">Kapat</button>`:""}</div>`:""}
      <input id="gq" type="search" placeholder="Oyun ara" autocomplete="off"></header><div class="grid" id="gg"></div></div>`;
    const r2=()=>{const q=($("#gq").value||"").toLowerCase();$("#gg").innerHTML=games.filter(g=>!g.hidden&&g.name.toLowerCase().includes(q)).map(g=>`<button class="game" data-id="${g.appid}"><img loading="lazy" alt="" src="${img(g.appid)}"><span>${esc(g.name)}</span><em>${rel(g.last)}</em></button>`).join("")||`<div class="empty">Oyun yok.</div>`;
      $("#gg").querySelectorAll(".game").forEach(b=>b.onclick=async()=>{const g=games.find(x=>x.appid===b.dataset.id);if(!confirm(g.name+" açılsın mı?"))return;
        const r=await post("/api/launch",{appid:g.appid});toast(r.ok?"Başlatılıyor: "+g.name:"Açılamadı.",r.ok?"ok":"err");setTimeout(draw,8000)})};
    $("#gq").oninput=r2;r2();
    $("#gout").onclick=()=>{try{localStorage.removeItem("pin")}catch(e){}pin="";games=[];loginView()};
    const s=$("#gstop");if(s)s.onclick=async()=>{if(!confirm("Oyun kapatılsın mı?"))return;await post("/api/stop");toast("Kapatıldı.","ok");draw()}};
  draw();
}
/* ---- Ayarlar: misafir + adres/QR ---- */
async function loadAddr(){
  let a;try{a=await(await fetch("/api/addr",{headers:H()})).json()}catch(e){return}
  const el=$("#addrc");if(!el)return;
  const L=[["Evde (Wi-Fi)",a.lan],["Dışarıda (Tailscale)",a.ts],["Güvenli bağlantı (mikrofon için)",a.https]].filter(x=>x[1]);
  el.innerHTML=`<h2>Adres & QR kod</h2><p class="note" style="margin-bottom:10px">Başka bir telefonla bağlanmak için QR'ı okut. PC'de büyük göstermek için PC tarayıcısında <b>localhost:${location.port||8765}/qr</b> adresini aç.</p>
    ${L.map(([t,u],i)=>`<div class="top" style="align-items:flex-start"><div><div class="tn"><span>${t}</span></div><span class="note" style="word-break:break-all">${esc(u)}</span><div class="row" style="margin:6px 0 0"><button class="chip" data-cp="${esc(u)}">Kopyala</button><button class="chip" data-qr="${i}">QR göster</button></div><div id="qrb${i}" style="margin-top:8px"></div></div></div>`).join("")}
    <div class="card" style="background:var(--bg);margin:12px 0 0"><h2>Misafir girişi</h2><p class="note" style="margin-bottom:8px">Misafir PIN'iyle giren biri sadece oyun ${a.guest_stop?"açıp kapatabilir":"açabilir"}; ekran, dosyalar, güç ve ayarlar kapalıdır. ${a.guest?"<b style='color:var(--ok)'>Şu an açık.</b>":"Şu an kapalı."}</p>
    <input type="password" inputmode="numeric" id="gpin" placeholder="${a.guest?"Yeni misafir PIN'i":"Misafir PIN'i (4–12 rakam)"}" autocomplete="off"><div class="row" style="flex-wrap:wrap;margin:0 0 8px"><button class="chip${a.guest_stop?" on":""}" id="gstp">Misafir oyunu kapatabilsin</button></div>
    <div class="btns"><button class="btn" id="gsv">${a.guest?"PIN'i değiştir":"Misafir girişini aç"}</button>${a.guest?`<button class="btn bad" id="goff">Kapat</button>`:""}</div></div>`;
  el.querySelectorAll("[data-cp]").forEach(b=>b.onclick=()=>copyText(b.dataset.cp));
  el.querySelectorAll("[data-qr]").forEach(b=>b.onclick=()=>showQR(L[+b.dataset.qr][1],$("#qrb"+b.dataset.qr)));
  let gs=a.guest_stop;$("#gstp").onclick=async e=>{gs=!gs;e.currentTarget.classList.toggle("on",gs);await post("/api/guest",{stop:gs})};
  $("#gsv").onclick=async()=>{const v=$("#gpin").value.trim();if(!/^\d{4,12}$/.test(v)){toast("PIN 4–12 rakam olmalı.","err");return}const r=await post("/api/guest",{pin:v,stop:gs});toast(r.ok?"Misafir girişi açık.":"Ana PIN'den farklı olmalı.",r.ok?"ok":"err");if(r.ok)loadAddr()};
  const go=$("#goff");if(go)go.onclick=async()=>{await post("/api/guest",{pin:""});toast("Misafir girişi kapatıldı.","ok");loadAddr()};
}
function showQR(u,box){
  const mk=()=>{try{const q=qrcode(0,"M");q.addData(u);q.make();box.innerHTML=`<div style="background:#fff;padding:10px;border-radius:12px;display:inline-block">${q.createSvgTag({cellSize:5,margin:0})}</div>`}catch(e){box.textContent="QR oluşturulamadı."}};
  if(window.qrcode)return mk();
  const s=document.createElement("script");s.src="https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js";s.onload=mk;s.onerror=()=>{box.textContent="QR için telefonun internete bağlı olmalı."};document.head.appendChild(s);
}
/* ---- Sohbet ---- */
let chatOpen=null,chatSt=null,chatLast=0,chatMsgs=[];
const tm2=t=>{const d=new Date(t*1e3),n=new Date();return d.toDateString()===n.toDateString()?d.toLocaleTimeString("tr-TR",{hour:"2-digit",minute:"2-digit"}):d.toLocaleDateString("tr-TR",{day:"numeric",month:"short"})};
const cav=(n,room)=>`<div class="cav${room?" room":""}" style="background:hsl(${[...(n||"?")].reduce((a,c)=>a+c.charCodeAt(0),0)%360} 35% 32%)">${esc((n||"?")[0].toUpperCase())}</div>`;
const CERR={limit:"Günlük mesaj sınırına ulaşıldı (ntfy günde 250 mesaja izin veriyor). Yarın tekrar dene.",net:"Gönderilemedi. İnternet bağlantısını kontrol et.",big:"Resim çok büyük.",code:"Kod geçersiz.",already:"Zaten arkadaşsınız.",conv:"Sohbet bulunamadı.",game:"Oyun bulunamadı."};
let _lastUnread=-1;
async function chatBadge(){
  if(ME&&ME.standalone)return;
  try{const r=await fetch("/api/chat",{headers:H()});if(!r.ok)return;chatSt=await r.json();const b=$("#nbc");if(!b)return;
    const n=chatSt.unread||0;if(_lastUnread>=0&&n>_lastUnread&&!chatOpen)sfx("msg");_lastUnread=n;
    b.hidden=!n;b.textContent=n>9?"9+":n;if(tab==="chat"&&!chatOpen&&$("#cl"))drawChatList()}catch(e){}
}
setInterval(()=>{if(!document.hidden&&games.length&&!chatOpen)chatBadge()},6000);
async function chatView(){
  $("#main").innerHTML=`<header><h1>Sohbet</h1></header><div id="cl"><div class="empty">Yükleniyor…</div></div>`;
  await chatBadge();if(tab==="chat")drawChatList();
}
function drawChatList(){
  const el=$("#cl");if(!el||!chatSt)return;const st=chatSt;
  if(!st.me){el.innerHTML=`<div class="card"><h2>Sohbete başla</h2><p class="note" style="margin-bottom:10px">Arkadaşlarınla mesajlaşabilir, oda kurup grup sohbeti yapabilir, birbirinizi oyuna çağırabilirsiniz. Arkadaşlarının da kendi PC'lerinde bu programı çalıştırması gerekir. Mesajlar şifreli gider.</p>
    <input type="text" id="cnm" maxlength="30" value="${esc(st.suggest||"")}" placeholder="Görünen adın"><button class="btn" style="width:100%" id="cgo">Başla</button></div>`;
    $("#cgo").onclick=async()=>{await post("/api/chat",{action:"init",name:$("#cnm").value});chatView()};return}
  const g=c=>c.kind==="dm"&&c.game?`<span class="g">🎮 ${esc(c.game.name)}</span>`:"";
  const sub=c=>c.last?`<span>${c.last.mine?"Sen: ":c.kind==="room"&&c.last.n?esc(c.last.n)+": ":""}${esc(c.last.text||"")}</span>`:`<span>${c.kind==="room"?(c.members||[]).length+" kişi · kod: "+c.code:"Henüz mesaj yok"}</span>`;
  if(!ME)meLoad().then(()=>{if(tab==="chat"&&!chatOpen)drawChatList()});
  el.innerHTML=(st.ok?"":`<p class="note" style="margin:0 0 10px;color:var(--warn)">Sohbet sunucusuna bağlanılamıyor, yeniden deneniyor…</p>`)+
    (st.reqs.length?`<div class="card"><h2>Arkadaşlık istekleri</h2>${st.reqs.map(r=>`<div class="cv">${cav(r.name||"?")}<div><b>${esc(r.name||r.code)}</b><span>${r.dir==="in"?"Seni eklemek istiyor":"İstek gönderildi, onay bekleniyor · "+r.code}</span></div>${r.dir==="in"?`<button class="chip on" data-ra="${r.code}">Kabul</button><button class="chip" data-rr="${r.code}">Reddet</button>`:`<button class="chip" data-rr="${r.code}">İptal</button>`}</div>`).join("")}</div>`:"")+
    `<div class="card" style="padding:4px 14px">${st.convs.length?st.convs.map(c=>`<button class="cv" data-cv="${c.id}">${c.prof&&ME?avatarHTML(c.prof,40):cav(c.name,c.kind==="room")}<div><b>${esc(c.name)}</b>${g(c)||sub(c)}</div><i>${c.last?tm2(c.last.ts):""}${c.unread?`<em>${c.unread}</em>`:""}</i></button>`).join(""):`<p class="note" style="padding:12px 0">Henüz sohbetin yok. Aşağıdan arkadaş ekle ya da oda kur.</p>`}</div>
    <div class="btns" style="margin-bottom:12px"><button class="btn" id="cadd">Arkadaş ekle</button><button class="btn" id="cmk">Oda kur</button><button class="btn" id="cjn">Odaya katıl</button></div>
    <div class="card"><h2>Arkadaş kodun</h2><div class="code">${st.me.code}</div><p class="note" style="margin-bottom:10px">Bu kodu arkadaşına gönder, o da Sohbet > Arkadaş ekle'ye yazsın. Sen de onun kodunu ekleyebilirsin.</p>
    <div class="btns"><button class="btn" id="ccp">Kodu kopyala</button>${navigator.share?`<button class="btn" id="csh">Paylaş</button>`:""}<button class="btn" id="cren">Adını değiştir (${esc(st.me.name)})</button></div></div>`;
  el.querySelectorAll("[data-cv]").forEach(b=>b.onclick=()=>openConv(b.dataset.cv));
  el.querySelectorAll("[data-ra]").forEach(b=>b.onclick=()=>chatAct({action:"accept",code:b.dataset.ra},"Artık arkadaşsınız."));
  el.querySelectorAll("[data-rr]").forEach(b=>b.onclick=()=>chatAct({action:"reject",code:b.dataset.rr},""));
  $("#ccp").onclick=()=>copyText(st.me.code);if($("#csh"))$("#csh").onclick=()=>navigator.share({text:`Steam Uzaktan arkadaş kodum: ${st.me.code}`}).catch(()=>{});
  $("#cren").onclick=()=>{const n=prompt("Görünen adın:",st.me.name);if(n)chatAct({action:"setname",name:n},"Ad değişti.")};
  $("#cadd").onclick=()=>{const c=prompt("Arkadaşının kodu (SR-XXXXX-XXXXX):");if(c)chatAct({action:"addfriend",code:c.trim().toUpperCase()},"İstek gönderildi. Arkadaşın kabul edince sohbet açılır.")};
  $("#cmk").onclick=async()=>{const n=prompt("Oda adı:","Oyun ekibi");if(n===null)return;const j=await chatAct({action:"mkroom",name:n},"");if(j&&j.ok){openConv(j.id);setTimeout(()=>alert("Oda kuruldu. Arkadaşlarına bu kodu gönder:\n\n"+j.code),300)}};
  $("#cjn").onclick=async()=>{const c=prompt("Oda kodu (ODA-XXXX-XXXX):");if(!c)return;const j=await chatAct({action:"join",code:c.trim().toUpperCase()},"Odaya katıldın.");if(j&&j.ok)openConv(j.id)};
}
function copyText(t){const i=document.createElement("input");i.value=t;document.body.appendChild(i);i.select();i.setSelectionRange(0,999);try{document.execCommand("copy");toast("Kopyalandı.","ok")}catch(e){toast(t)}i.remove()}
async function chatAct(o,okMsg){
  try{const j=await(await post("/api/chat",o)).json();if(j.error){toast(CERR[j.error]||"Yapılamadı.","err");return j}if(okMsg)toast(okMsg,"ok");await chatBadge();if(!chatOpen&&tab==="chat")drawChatList();return j}
  catch(e){toast("PC'ye ulaşılamadı.","err")}
}
async function openConv(id){
  chatOpen=id;chatLast=0;chatMsgs=[];document.body.classList.add("inchat");
  const c=(chatSt&&chatSt.convs.find(x=>x.id===id))||{name:"Sohbet",kind:id[0]==="r"?"room":"dm",code:id.slice(2)};
  const subt=c.kind==="room"?(c.members||[]).map(m=>esc(m.n)+(m.g?" 🎮":"")).join(", ")||"Kod: "+c.code:c.game?"🎮 "+esc(c.game.name)+" oynuyor":"Arkadaş";
  $("#main").innerHTML=`<header><div class="row" style="margin:0"><button class="chip" id="cbk">‹</button><div style="flex:1;min-width:0"><h1 style="margin:0;font-size:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(c.name)}</h1><span class="note" style="font-size:12px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${subt}</span></div><button class="chip" id="cmn">⋯</button></div></header>
  <div id="msgs"><div class="empty">Yükleniyor…</div></div>
  <div class="cmp"><button id="cplus" aria-label="Ekle">➕</button><button id="cmic" aria-label="Sesli mesaj">🎤</button><input type="text" id="ctx" maxlength="1000" placeholder="Mesaj yaz" autocomplete="off"><button class="snd" id="csd" aria-label="Gönder">➤</button><input type="file" id="cfi" accept="image/*" hidden></div>`;
  $("#cbk").onclick=()=>{tab="chat";show()};
  const send=async()=>{const t=$("#ctx").value.trim();if(!t)return;$("#ctx").value="";const j=await chatAct({action:"send",id,text:t},"");if(j&&j.error)$("#ctx").value=t;pullMsgs()};
  $("#csd").onclick=send;$("#ctx").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();send()}};
  $("#cfi").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(f)await sendImg(id,f)};
  $("#cplus").onclick=()=>plusSheet(id);micBind(id);msgDelegates(id);
  $("#cmn").onclick=()=>convMenu(c);
  await pullMsgs();
  const t=setInterval(()=>{if(chatOpen!==id){clearInterval(t);return}if(!document.hidden)pullMsgs()},2500);
}
function convMenu(c){
  const d=bigSheet(esc(c.name)),b=d.querySelector(".sb");
  b.innerHTML=`<div class="btns" style="grid-template-columns:1fr">${c.kind==="room"?`<div class="card" style="margin:0"><h2>Oda kodu</h2><div class="code">${c.code}</div><p class="note">Arkadaşların bu kodla Sohbet > Odaya katıl'dan girebilir.</p></div><button class="btn" id="mcp">Kodu kopyala</button><button class="btn" id="mrn">Odanın adını değiştir</button>${(c.members||[]).length?`<p class="note" style="margin:6px 0 0">Odadakiler: ${(c.members||[]).map(m=>esc(m.n)+(m.g?" (🎮 "+esc(m.g.name)+")":"")).join(", ")}</p>`:""}<button class="btn bad" id="mlv">Odadan çık</button>`:`<p class="note">Arkadaş kodu: ${c.code}</p><button class="btn" id="mprof">Profiline bak</button><button class="btn" id="mgift">🎁 Hediye gönder</button><button class="btn" id="mrps">✊ Taş-Kağıt-Makas</button><button class="btn bad" id="muf">Arkadaşlıktan çıkar</button>`}</div>`;
  const on=(s,f)=>{const e=$(s);if(e)e.onclick=f};
  on("#mcp",()=>copyText(c.code));
  on("#mrn",async()=>{const n=prompt("Yeni ad:",c.name);if(n){await chatAct({action:"rename",id:c.id,name:n},"Ad değişti.");d.remove()}});
  on("#mlv",async()=>{if(!confirm("Odadan çıkılsın mı? Geçmiş bu cihazdan silinir."))return;d.remove();await chatAct({action:"leave",id:c.id},"Odadan çıktın.");tab="chat";show()});
  on("#mprof",()=>{d.remove();friendProfile(c)});
  on("#mgift",()=>{d.remove();giftSheet(c.id)});
  on("#mrps",()=>{d.remove();rpsSheet(c.id)});
  on("#muf",async()=>{if(!confirm(c.name+" arkadaşlıktan çıkarılsın mı?"))return;d.remove();await chatAct({action:"unfriend",code:c.code},"Çıkarıldı.");tab="chat";show()});
}
function friendProfile(c){
  post("/api/chat",{action:"visit",code:c.code}).catch(()=>{});
  const p=c.prof;const d=bigSheet(esc(c.name)),b=d.querySelector(".sb");
  const tile=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  if(!p){b.innerHTML=`<div class="pb" style="background:#223247">${cav(c.name)}<h1>${esc(c.name)}</h1><p>Profilini henüz paylaşmadı.</p></div>`;return}
  const fg=p.fav&&games.find(g=>g.appid===(p.fav.appid||p.fav));
  b.innerHTML=bannerHTML(p,`<div class="lv">Seviye ${p.level||1}</div>`)+
    `<div class="card" style="background:var(--bg)"><div class="sts">${tile((p.hours||0)+" sa","Toplam oyun")+tile((p.items||0),"Eşya")+tile(c.code,"Kod")+tile((p.badges||[]).length,"Rozet")}</div>${p.fav?`<p class="note" style="margin-top:10px">Favori oyunu: <b style="color:var(--text)">${esc(p.fav.name||"")}</b></p>`:""}</div>`;
}
const RX=["👍","😂","🔥","❤️","😮","😢"];
function msgHTML(m,room){
  const who=room&&!m.me?`<small>${esc(m.from.n||"?")}</small>`:"";
  const rx=m.re&&Object.keys(m.re).length?(()=>{const c={};Object.values(m.re).forEach(e=>c[e]=(c[e]||0)+1);const mine=m.re[m.myc];return `<div class="rx">${Object.entries(c).map(([e,n])=>`<span class="${e===mine?"on":""}">${e}${n>1?" "+n:""}</span>`).join("")}</div>`})():"";
  const mm=m.me?" mine":"";
  if(m.t==="join"||m.t==="leave")return `<div class="sys" data-mid="${m.mid}">${esc(m.from.n||"Biri")} ${m.t==="join"?"odaya katıldı":"odadan ayrıldı"} · ${tm2(m.ts)}</div>`;
  if(m.t==="invite"&&m.game){const has=games.some(g=>g.appid===m.game.appid);
    return `<div class="inv${mm}" data-mid="${m.mid}"><img alt="" src="${img(m.game.appid)}"><div><b>${esc(m.game.name)}</b><p>${m.me?"Oyuna davet gönderdin":esc(m.from.n||"Arkadaşın")+" seni oynamaya çağırıyor"}${m.text?": "+esc(m.text):""} · ${tm2(m.ts)}</p>${m.me?"":`<div class="btns"><button class="btn" data-ij="${m.game.appid}" data-in="${esc(m.game.name)}">${has?"Oyunu aç":"Kur"}</button><button class="btn" data-ig="1">Geliyorum!</button></div>`}${rx}</div></div>`}
  if(m.t==="event"){const rs=m.rsvp||{},my=(rs[m.myc]||{}).a,L=a=>Object.values(rs).filter(r=>r.a===a).map(r=>esc(r.n));const past=m.when*1e3<Date.now();
    const when=new Date(m.when*1e3).toLocaleString("tr-TR",{weekday:"long",day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"});
    return `<div class="evt${mm}" data-mid="${m.mid}">${m.game?`<img alt="" src="${img(m.game.appid)}">`:""}<div><b>📅 ${esc(m.title||"Oyun gecesi")}</b><p>${when}${m.game?" · "+esc(m.game.name):""}<br>${esc(m.from.n||"")} planladı${past?" · başladı":""}</p>
      <div class="row" style="flex-wrap:wrap;margin:0 0 6px">${[["yes","✅ Katılıyorum"],["maybe","🤔 Belki"],["no","❌ Katılamam"]].map(([k,l])=>`<button class="chip${my===k?" on":""}" data-rsvp="${k}" data-ref="${m.mid}">${l}</button>`).join("")}</div>
      <p style="margin:0">${L("yes").length?"✅ "+L("yes").join(", "):""}${L("maybe").length?"<br>🤔 "+L("maybe").join(", "):""}${L("no").length?"<br>❌ "+L("no").join(", "):""}</p>${rx}</div></div>`}
  if(m.t==="poll"){const v=m.votes||{},tot=Object.keys(v).length,my=v[m.myc];
    return `<div class="pol${mm}" data-mid="${m.mid}"><div><b>📊 ${esc(m.q||"")}</b><p>${esc(m.from.n||"")} · ${tot} oy · ${tm2(m.ts)}</p>${(m.opts||[]).map((o,i)=>{const n=Object.values(v).filter(x=>x===i).length,pc=tot?Math.round(n/tot*100):0;return `<button class="opt${my===i?" on":""}" data-vote="${i}" data-ref="${m.mid}"><i style="width:${pc}%"></i><span>${esc(o)}</span><em>${n?pc+"%":""}</em></button>`}).join("")}${rx}</div></div>`}
  if(m.t==="gift")return `<div class="gift" data-mid="${m.mid}"><b>🎁</b><span>${m.me?"Sen":esc(m.from.n||"Arkadaşın")} ${esc((m.text||"hediye etti").replace(/^🎁 /,""))}</span><br><time style="opacity:.7;font-size:11px">${tm2(m.ts)}</time></div>`;
  if(m.t==="rps"){const RPL={tas:"✊",kagit:"✋",makas:"✌️"};const done=m.done,my=m.me;
    let body;
    if(done){const w=m.result,iWon=my?w===1:w===2;const ca=m.cChal,cb=m.cAcc;
      body=`<div>${RPL[ca]||"?"} vs ${RPL[cb]||"?"}</div><div class="res" style="color:${w===0?"var(--muted)":iWon?"var(--ok)":"var(--bad)"}">${w===0?"Berabere":iWon?"Kazandın! +"+m.bet+" 🪙":"Kaybettin −"+m.bet+" 🪙"}</div>`;}
    else if(m.cB){body=`<p class="note">${my?"Rakibin seçti, sonuç hesaplanıyor…":"Seçimin alındı, sonuç bekleniyor…"}</p>`;}
    else if(my){body=`<p class="note">Rakibinin kabul etmesi bekleniyor…</p>`;}
    else{body=`<div class="pick">${Object.keys(RPL).map(k=>`<button data-rps="${k}" data-ref="${m.mid}">${RPL[k]}</button>`).join("")}</div><p class="note" style="font-size:12px">Seç — kazanan ${m.bet} jeton kazanır.</p>`;}
    return `<div class="rps${mm}" data-mid="${m.mid}"><b>✊✋✌️ Taş-Kağıt-Makas · ${m.bet} 🪙</b>${body}<time style="display:block;font-size:11px;opacity:.6;margin-top:4px">${tm2(m.ts)}</time></div>`;}
  if(m.t==="voice")return `<div class="bub${mm}" data-mid="${m.mid}">${who}${m.audio?`<audio controls preload="none" src="${m.audio}"></audio>`:esc(m.text||"Sesli mesaj")}<time>🎤 ${m.dur?m.dur+" sn · ":""}${tm2(m.ts)}</time>${rx}</div>`;
  if(m.t==="img")return `<div class="bub ph${mm}" data-mid="${m.mid}">${who}${m.img?`<img alt="Resim" src="${m.img}" data-vi="${m.img}">`:`<span style="padding:8px">${esc(m.text||"Resim")}</span>`}<time>${tm2(m.ts)}</time>${rx}</div>`;
  return `<div class="bub${mm}" data-mid="${m.mid}">${who}${esc(m.text||"")}<time>${tm2(m.ts)}${m.g&&!m.me?" · 🎮 "+esc(m.g.name):""}</time>${rx}</div>`;
}
async function pullMsgs(){
  const id=chatOpen;if(!id)return;
  let L;try{L=await(await fetch(`/api/chat/msgs?id=${encodeURIComponent(id)}&after=${chatLast}`,{headers:H()})).json()}catch(e){return}
  if(chatOpen!==id)return;
  const box=$("#msgs");if(!box)return;
  if(!L.length){if(!chatMsgs.length)box.innerHTML=`<div class="empty">Henüz mesaj yok. İlk mesajı sen yaz!</div>`;return}
  if(!chatMsgs.length)box.innerHTML="";
  const near=window.innerHeight+window.scrollY>=document.body.scrollHeight-160||!chatLast;
  const room=id[0]==="r";let added=false;
  for(const m of L){
    chatLast=Math.max(chatLast,m._u||m.ts);
    const i=chatMsgs.findIndex(x=>x.mid===m.mid);
    if(i>=0){chatMsgs[i]=m;const el=box.querySelector(`[data-mid="${m.mid}"]`);if(el)el.outerHTML=msgHTML(m,room);continue}
    chatMsgs.push(m);box.insertAdjacentHTML("beforeend",msgHTML(m,room));added=true;
  }
  if(near&&added)setTimeout(()=>window.scrollTo(0,document.body.scrollHeight),50);
  post("/api/chat",{action:"read",id}).catch(()=>{});
}
function msgDelegates(id){
  const box=$("#msgs");if(!box)return;
  box.onclick=e=>{const t=e.target.closest("button,img[data-vi]");if(!t)return;
    if(t.dataset.ij){const a=t.dataset.ij;games.some(g=>g.appid===a)?launch(a,t.dataset.in,null):steamApp(a,"install",t.dataset.in)}
    else if(t.dataset.ig)chatAct({action:"send",id,text:"Geliyorum! 🎮"},"").then(pullMsgs);
    else if(t.dataset.rsvp)chatAct({action:"rsvp",id,ref:t.dataset.ref,ans:t.dataset.rsvp},"Cevabın gönderildi.").then(pullMsgs);
    else if(t.dataset.vote!=null&&t.dataset.ref)chatAct({action:"vote",id,ref:t.dataset.ref,i:+t.dataset.vote},"Oyun verildi.").then(pullMsgs);
    else if(t.dataset.rps){const code=id.slice(2);chatAct({action:"rpsmove",id,ref:t.dataset.ref,choice:t.dataset.rps},"Seçimin gönderildi.").then(pullMsgs)}
    else if(t.dataset.vi){const v=document.createElement("div");v.id="viewer";v.innerHTML=`<img alt="" src="${t.dataset.vi}"><div class="vb"><span style="flex:1;font-size:11px;opacity:.7">Kaydetmek için resme basılı tut.</span><button class="chip on" id="vx">Kapat</button></div>`;document.body.appendChild(v);$("#vx").onclick=()=>v.remove()}};
  let tm=null,sx=0,sy=0;
  const start=e=>{const el=e.target.closest("[data-mid]");if(!el||el.classList.contains("sys")||e.target.closest("button,audio"))return;const p=e.touches?e.touches[0]:e;sx=p.clientX;sy=p.clientY;
    tm=setTimeout(()=>{tm=null;reactPick(id,el.dataset.mid)},480)};
  const cancel=e=>{if(!tm)return;const p=e.touches?e.touches[0]:e;if(!e.touches||Math.hypot(p.clientX-sx,p.clientY-sy)>10){clearTimeout(tm);tm=null}};
  box.addEventListener("touchstart",start,{passive:true});box.addEventListener("touchmove",cancel,{passive:true});box.addEventListener("touchend",()=>{clearTimeout(tm);tm=null});
  box.addEventListener("contextmenu",e=>{const el=e.target.closest("[data-mid]");if(el&&!el.classList.contains("sys")){e.preventDefault();reactPick(id,el.dataset.mid)}});
}
function reactPick(id,mid){
  const d=bigSheet("Tepki bırak"),b=d.querySelector(".sb");
  b.innerHTML=`<div class="rpk">${RX.map(e=>`<button data-e="${e}">${e}</button>`).join("")}</div><p class="note" style="text-align:center;margin-top:8px">Aynı tepkiye tekrar basarsan kaldırılır.</p>`;
  b.querySelectorAll("[data-e]").forEach(x=>x.onclick=async()=>{d.remove();await chatAct({action:"react",id,ref:mid,emoji:x.dataset.e},"");pullMsgs()});
}
function plusSheet(id){
  const d=bigSheet("Gönder"),b=d.querySelector(".sb");
  const dm=id[0]==="d";
  b.innerHTML=`<div class="btns" style="grid-template-columns:1fr 1fr"><button class="btn" data-pl="img">📷 Resim</button><button class="btn" data-pl="inv">🎮 Oyuna davet</button><button class="btn" data-pl="evt">📅 Oyun gecesi</button><button class="btn" data-pl="poll">📊 Anket</button>${dm?`<button class="btn" data-pl="gift">🎁 Hediye</button><button class="btn" data-pl="rps">✊ Taş-Kağıt-Makas</button>`:""}</div>`;
  b.querySelectorAll("[data-pl]").forEach(x=>x.onclick=()=>{d.remove();({img:()=>$("#cfi").click(),inv:()=>inviteSheet(id),evt:()=>eventSheet(id),poll:()=>pollSheet(id),gift:()=>giftSheet(id),rps:()=>rpsSheet(id)})[x.dataset.pl]()});
}
function eventSheet(id){
  const d=bigSheet("📅 Oyun gecesi planla"),b=d.querySelector(".sb");
  const t=new Date();t.setHours(21,0,0,0);if(t<new Date())t.setDate(t.getDate()+1);
  const pad=n=>String(n).padStart(2,"0"),dv=`${t.getFullYear()}-${pad(t.getMonth()+1)}-${pad(t.getDate())}`;
  const sel=`width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px`;
  b.innerHTML=`<input type="text" id="evn" maxlength="80" placeholder="Başlık (örn. Cuma CS2 gecesi)" style="width:100%">
    <select id="evg" style="${sel}"><option value="">Oyun seç (isteğe bağlı)</option>${games.filter(g=>!g.hidden).map(g=>`<option value="${g.appid}">${esc(g.name)}</option>`).join("")}</select>
    <div class="row" style="margin:0 0 8px"><input type="date" id="evd" value="${dv}" style="${sel};margin:0;flex:1"><input type="time" id="evt" value="21:00" style="${sel};margin:0;width:120px"></div>
    <p class="note" style="font-size:12px;margin-bottom:10px">Katılanlara 15 dakika önce ve saatinde bildirim gider.</p>
    <button class="btn" style="width:100%;background:var(--accent);color:#0b1a28;border:0" id="evs">Planla ve paylaş</button>`;
  $("#evg").onchange=e=>{if(!$("#evn").value&&e.target.value){const g=games.find(x=>x.appid===e.target.value);$("#evn").value=g.name+" gecesi"}};
  $("#evs").onclick=async()=>{const title=$("#evn").value.trim(),when=Math.floor(new Date($("#evd").value+"T"+$("#evt").value).getTime()/1e3);
    if(!title){toast("Başlık yaz.","err");return}if(!when||when*1e3<Date.now()-60000){toast("Geçmiş bir zaman seçtin.","err");return}
    d.remove();const j=await chatAct({action:"event",id,title,when,appid:$("#evg")?$("#evg").value:""},"Oyun gecesi paylaşıldı.");pullMsgs()};
}
function pollSheet(id){
  const d=bigSheet("📊 Anket"),b=d.querySelector(".sb");
  b.innerHTML=`<input type="text" id="pq" maxlength="120" placeholder="Soru (örn. Bu akşam ne oynayalım?)" style="width:100%">${[1,2,3,4].map(i=>`<input type="text" class="po" maxlength="60" placeholder="Seçenek ${i}${i>2?" (isteğe bağlı)":""}" style="width:100%">`).join("")}
    <div class="row" style="flex-wrap:wrap;margin:0 0 10px"><button class="chip" id="pgm">Oyunlarımdan doldur</button></div><button class="btn" style="width:100%;background:var(--accent);color:#0b1a28;border:0" id="ps">Anketi gönder</button>`;
  $("#pgm").onclick=()=>{const L=games.filter(g=>g.last&&!g.hidden).slice(0,4);document.querySelectorAll(".po").forEach((x,i)=>{if(L[i])x.value=L[i].name});if(!$("#pq").value)$("#pq").value="Bu akşam ne oynayalım?"};
  $("#ps").onclick=async()=>{const q=$("#pq").value.trim(),opts=[...document.querySelectorAll(".po")].map(x=>x.value.trim()).filter(Boolean);
    if(!q||opts.length<2){toast("Soru ve en az 2 seçenek yaz.","err");return}d.remove();await chatAct({action:"poll",id,q,opts},"Anket gönderildi.");pullMsgs()};
}
function micBind(id){
  const b=$("#cmic");if(!b)return;
  if(!window.isSecureContext||!navigator.mediaDevices){b.onclick=()=>{if(confirm("Sesli mesaj için telefon mikrofonu gerekir; iPhone bunu sadece güvenli bağlantıda verir.\n\nGüvenli bağlantı açılsın mı? (\"Bağlantı gizli değil\" uyarısında Ayrıntılar > Bu web sitesini ziyaret et de, PIN'ini bir kez gir.)"))location.href=`https://${location.hostname}:${(+location.port||80)+1}/`};return}
  let rec=null,t0=0;
  const start=e=>{e.preventDefault();if(rec)return;t0=Date.now();b.classList.add("rec");toast("Kaydediliyor… bırakınca gönderilir");const pr=recStart();rec={p:pr};pr.catch(()=>{rec=null;b.classList.remove("rec");toast("Mikrofon izni verilmedi.","err")})};
  const stop=async e=>{e.preventDefault();if(!rec)return;const pr=rec.p;rec=null;b.classList.remove("rec");let r;try{r=await pr}catch(x){return}
    const dur=Math.round((Date.now()-t0)/1e3),wav=await r.stop();if(!wav){toast("Çok kısa, biraz daha uzun basılı tut.");return}
    if(wav.size>1600*1024){toast("Sesli mesaj en fazla ~45 saniye olabilir.","err");return}
    try{const j=await(await fetch(`/api/chat/voice?id=${encodeURIComponent(id)}&dur=${dur}`,{method:"POST",headers:{"X-Pin":pin,"Content-Type":"audio/wav"},body:wav})).json();toast(j.error?(CERR[j.error]||"Gönderilemedi."):"Sesli mesaj gönderildi.",j.error?"err":"ok")}catch(x){toast("Gönderilemedi.","err")}
    pullMsgs()};
  b.addEventListener("touchstart",start,{passive:false});b.addEventListener("touchend",stop,{passive:false});b.addEventListener("touchcancel",stop,{passive:false});
  b.addEventListener("mousedown",start);b.addEventListener("mouseup",stop);
}
async function shrinkImg(src){
  const im=new Image();im.src=src;await im.decode();
  for(const [mx,q] of [[1600,.82],[1280,.72],[1024,.62],[800,.55]]){
    const k=Math.min(1,mx/Math.max(im.naturalWidth,im.naturalHeight)),c=document.createElement("canvas");
    c.width=Math.round(im.naturalWidth*k);c.height=Math.round(im.naturalHeight*k);c.getContext("2d").drawImage(im,0,0,c.width,c.height);
    const b=await new Promise(r=>c.toBlob(r,"image/jpeg",q));if(b&&b.size<1900*1024)return b}
  return null;
}
async function sendImg(id,file){
  toast("Resim hazırlanıyor…");const u=URL.createObjectURL(file);let b;try{b=await shrinkImg(u)}catch(e){}URL.revokeObjectURL(u);
  if(!b){toast("Resim açılamadı.","err");return}
  try{const j=await(await fetch("/api/chat/img?id="+encodeURIComponent(id),{method:"POST",headers:{"X-Pin":pin,"Content-Type":"image/jpeg"},body:b})).json();
    if(j.error)toast(CERR[j.error]||"Gönderilemedi.","err");else toast("Resim gönderildi.","ok")}catch(e){toast("Gönderilemedi.","err")}
  if(chatOpen===id)pullMsgs();
}
async function shareToChat(src){
  await chatBadge();const cs=chatSt&&chatSt.convs||[];
  if(!chatSt||!chatSt.me||!cs.length){toast("Önce Sohbet sekmesinden bir arkadaş ekle ya da oda kur.","err");return}
  const d=bigSheet("Sohbette paylaş"),b=d.querySelector(".sb");
  b.innerHTML=cs.map(c=>`<button class="cv" data-sc="${c.id}">${cav(c.name,c.kind==="room")}<div><b>${esc(c.name)}</b></div></button>`).join("");
  b.querySelectorAll("[data-sc]").forEach(x=>x.onclick=async()=>{d.remove();const v=$("#viewer");if(v)v.remove();toast("Resim hazırlanıyor…");
    let bl;try{bl=await shrinkImg(src)}catch(e){}if(!bl){toast("Resim hazırlanamadı.","err");return}
    try{const j=await(await fetch("/api/chat/img?id="+encodeURIComponent(x.dataset.sc),{method:"POST",headers:{"X-Pin":pin,"Content-Type":"image/jpeg"},body:bl})).json();toast(j.error?(CERR[j.error]||"Gönderilemedi."):"Paylaşıldı.",j.error?"err":"ok")}catch(e){toast("Gönderilemedi.","err")}});
}
function giftSheet(id){
  const code=id.slice(2);
  const d=bigSheet("🎁 Hediye gönder"),b=d.querySelector(".sb");
  const own=(ME&&ME.owned||[]).map(i=>(ME.catalog||[]).find(c=>c.id===i)).filter(c=>c&&c.p>0&&!c.custom);
  b.innerHTML=`<p class="note" style="margin-bottom:8px">Jeton hediye et (seninkinden düşülür). Jetonun: 🪙 ${ME?ME.coins:0}</p>
    <div class="row" style="flex-wrap:wrap;margin:0 0 8px">${[50,100,250,500].map(a=>`<button class="chip" data-gc="${a}">🪙 ${a}</button>`).join("")}<input type="number" id="gca" placeholder="Özel" min="1" style="width:90px;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text)"><button class="chip" id="gcs">Gönder</button></div>
    <p class="note" style="margin:12px 0 8px">Ya da sahip olduğun bir eşyayı hediye et (sende kalır, ona açılır):</p>
    <div class="row" style="flex-wrap:wrap;margin:0">${own.length?own.map(c=>`<button class="chip" data-gi="${c.id}">${c.e?c.e+" ":""}${esc(c.n)}</button>`).join(""):`<span class="note">Hediye edebileceğin eşya yok.</span>`}</div>`;
  const giveC=async a=>{if(!a||a<1)return;if(!confirm(a+" jeton hediye edilsin mi?"))return;d.remove();const j=await(await post("/api/chat",{action:"gift",code,kind:"coins",amt:a})).json();toast(j.error?(j.error==="coins"?"Jetonun yetmiyor.":"Gönderilemedi."):"Hediye gönderildi!",j.error?"err":"ok");if(!j.error){sfx("coin");pullMsgs()}};
  b.querySelectorAll("[data-gc]").forEach(x=>x.onclick=()=>giveC(+x.dataset.gc));
  $("#gcs").onclick=()=>giveC(+$("#gca").value);
  b.querySelectorAll("[data-gi]").forEach(x=>x.onclick=async()=>{d.remove();const j=await(await post("/api/chat",{action:"gift",code,kind:"item",item:x.dataset.gi})).json();toast(j.error?"Gönderilemedi.":"Hediye gönderildi!",j.error?"err":"ok");if(!j.error){sfx("buy");pullMsgs()}});
}
function rpsSheet(id){
  const code=id.slice(2);
  const d=bigSheet("✊ Taş-Kağıt-Makas"),b=d.querySelector(".sb");
  b.innerHTML=`<p class="note" style="margin-bottom:10px">Arkadaşına meydan oku. Kazanan bahsi alır. Jetonun: 🪙 ${ME?ME.coins:0}</p>
    <p class="note" style="margin:0 0 6px">Bahis</p><div class="row" style="flex-wrap:wrap;margin:0 0 10px">${[10,25,50,100].map(a=>`<button class="chip" data-rb="${a}">🪙 ${a}</button>`).join("")}<input type="number" id="rba" placeholder="Özel" min="1" style="width:90px;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text)"></div>
    <button class="btn" style="width:100%;background:var(--accent);color:#0b1a28;border:0" id="rbs">Meydan oku</button>`;
  let bet=50;b.querySelectorAll("[data-rb]").forEach(x=>x.onclick=()=>{bet=+x.dataset.rb;b.querySelectorAll("[data-rb]").forEach(y=>y.classList.toggle("on",y===x))});
  $("#rba").oninput=e=>bet=+e.target.value;
  $("#rbs").onclick=async()=>{if(!bet||bet<1){toast("Bahis gir.","err");return}d.remove();const j=await(await post("/api/chat",{action:"rps",code,bet})).json();toast(j.error?(j.error==="coins"?"Jetonun yetmiyor.":"Gönderilemedi."):"Meydan okuma gönderildi!",j.error?"err":"ok");if(!j.error)pullMsgs()};
}
function inviteSheet(id){
  const d=bigSheet("Oyuna davet et"),b=d.querySelector(".sb");
  const list=[...games.filter(g=>g.appid===running.appid),...games.filter(g=>g.appid!==running.appid&&!g.hidden)];
  b.innerHTML=`<input type="text" id="ivt" maxlength="200" placeholder="Not ekle (isteğe bağlı), örn. 10 dk sonra" style="width:100%;margin-bottom:10px">`+list.slice(0,40).map(g=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${esc(g.name)}</span></div>${g.appid===running.appid?`<span class="note" style="color:var(--ok)">Şu an açık</span>`:""}</div><button class="chip" data-iv="${g.appid}">Davet et</button></div>`).join("");
  b.querySelectorAll("[data-iv]").forEach(x=>x.onclick=async()=>{const t=$("#ivt").value;d.remove();await chatAct({action:"invite",id,appid:x.dataset.iv,text:t},"Davet gönderildi.");pullMsgs()});
}
/* ---- Ses cihazı + uygulama sesleri ---- */
async function audioCards(){
  try{const d=await(await fetch("/api/audio-devices",{headers:H()})).json();const el=$("#adev");if(el){
    if(d.devices&&d.devices.length>1){el.innerHTML=`<p class="note" style="margin:0 0 6px">Ses çıkışı</p><select id="adsel" style="width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px">${d.devices.map(x=>`<option value="${esc(x.id)}"${x.cur?" selected":""}>${esc(x.name)}</option>`).join("")}</select>`;
      $("#adsel").onchange=async e=>{const r=await post("/api/audio-device",{id:e.target.value});toast(r.ok?"Ses çıkışı değişti.":"Değiştirilemedi.",r.ok?"ok":"err")};}
    else el.innerHTML="";}
  }catch(e){}
  try{const d=await(await fetch("/api/app-volumes",{headers:H()})).json();const el=$("#avol");if(!el)return;
    if(!d.apps||!d.apps.length){el.innerHTML="";return}
    el.innerHTML=`<p class="note" style="margin:6px 0">Uygulama sesleri</p>`+d.apps.map(a=>`<div class="av2"><span>${esc(a.name)}</span><input type="range" min="0" max="100" value="${a.vol}" data-vp="${a.pid}"><button data-vm="${a.pid}" class="${a.mute?"on":""}">${a.mute?"🔇":"🔊"}</button></div>`).join("");
    el.querySelectorAll("[data-vp]").forEach(r=>{let t;r.oninput=()=>{clearTimeout(t);t=setTimeout(()=>post("/api/app-volume",{pid:+r.dataset.vp,vol:+r.value}),200)}});
    el.querySelectorAll("[data-vm]").forEach(b=>b.onclick=async()=>{const m=!b.classList.contains("on");b.classList.toggle("on",m);b.textContent=m?"🔇":"🔊";await post("/api/app-volume",{pid:+b.dataset.vm,mute:m})});
  }catch(e){}
}
/* ---- Makrolar ---- */
async function mkBind(){
  const draw=async()=>{let d;try{d=await(await fetch("/api/macros",{headers:H()})).json()}catch(e){return}const el=$("#mks");if(!el)return;
    el.innerHTML=(d.macros.length?`<div class="btns" style="margin-bottom:8px">${d.macros.map((m,i)=>`<button class="btn" data-mr="${i}">${esc(m.name)}</button>`).join("")}</div>${d.macros.map((m,i)=>`<div class="tn" style="margin:0 0 6px"><span>${esc(m.name)} · <span class="note">${esc(m.keys)}</span></span><button class="chip" data-md="${i}" style="color:var(--bad)">Sil</button></div>`).join("")}`:`<p class="note">Henüz makro yok. Kendi tuş kombinasyonunu düğme yap (örn. Ctrl+Shift+Esc, Alt+F4).</p>`);
    el.querySelectorAll("[data-mr]").forEach(b=>b.onclick=async()=>{const r=await post("/api/macros",{action:"run",i:+b.dataset.mr});toast(r.ok?"Gönderildi.":"Çalıştırılamadı.",r.ok?"ok":"err")});
    el.querySelectorAll("[data-md]").forEach(b=>b.onclick=async()=>{await post("/api/macros",{action:"del",i:+b.dataset.md});draw()})};
  $("#mkadd").onclick=()=>{const name=prompt("Düğme adı (örn. Görev Yöneticisi):");if(!name)return;const keys=prompt("Tuşlar (+ ile ayır, örn. Ctrl+Shift+Esc):");if(!keys)return;
    post("/api/macros",{action:"add",name,keys}).then(r=>r.json()).then(j=>{if(j.error)toast("Geçersiz tuş. Örn: Ctrl+Shift+Esc","err");else draw()})};
  draw();
}
/* ---- Yedekleme ---- */
async function bkBind(){
  const fsz=b=>b>=2**30?(b/2**30).toFixed(1)+" GB":b>=2**20?Math.round(b/2**20)+" MB":Math.round(b/1024)+" KB";
  const draw=async()=>{let d;try{d=await(await fetch("/api/backups",{headers:H()})).json()}catch(e){return}const el=$("#bks");if(!el)return;
    el.innerHTML=d.backups.length?d.backups.map((b,i)=>`<div class="top" style="align-items:flex-start"><div><div class="tn"><span>${esc(b.name)}${b.auto?" 🔄":""}</span></div><span class="note" style="word-break:break-all">${esc(b.src)}</span><span class="note">${b.exists?`${b.count} yedek · ${fsz(b.size||0)}${b.last?" · son: "+tm2(b.last):""} · her ${b.every} saatte`:"⚠️ Klasör bulunamadı"}</span><div class="row" style="margin:6px 0 0"><button class="chip" data-bn="${i}">Şimdi yedekle</button><button class="chip" data-bt="${i}">${b.auto?"Otoyu kapat":"Otoyu aç"}</button><button class="chip" data-bd="${i}" style="color:var(--bad)">Sil</button></div></div></div>`).join("")+`<button class="chip" id="bkopen" style="margin-top:8px">Yedek klasörünü PC'de aç</button>`:`<p class="note">Henüz yedek yok.</p>`;
    el.querySelectorAll("[data-bn]").forEach(b=>b.onclick=async()=>{b.textContent="Yedekleniyor…";const r=await(await post("/api/backup",{action:"run",...d.backups[+b.dataset.bn]})).json();toast(r.ok?"Yedek alındı.":r.error==="folder"?"Klasör bulunamadı.":"Yedeklenemedi.",r.ok?"ok":"err");draw()});
    el.querySelectorAll("[data-bt]").forEach(b=>b.onclick=async()=>{await post("/api/backup",{action:"toggle",i:+b.dataset.bt});draw()});
    el.querySelectorAll("[data-bd]").forEach(b=>b.onclick=async()=>{if(!confirm("Bu yedek görevi silinsin mi? (Alınmış yedekler kalır.)"))return;await post("/api/backup",{action:"del",i:+b.dataset.bd});draw()});
    const o=$("#bkopen");if(o)o.onclick=()=>{post("/api/open-folder",{path:d.dir});toast("PC'de açılıyor.","ok")}};
  $("#bkadd").onclick=()=>{const src=prompt("Yedeklenecek klasörün tam yolu (örn. C:\\Users\\X\\Documents\\My Games):");if(!src)return;const name=prompt("Bu yedeğe bir ad ver (örn. Elden Ring save):");if(!name)return;
    const every=prompt("Kaç saatte bir otomatik yedeklensin? (örn. 6)","6");
    post("/api/backup",{action:"add",src,name,every:+every||6,auto:true}).then(r=>r.json()).then(j=>{if(j.error)toast("Ad ve klasör gerekli.","err");else{toast("Eklendi, ilk yedek alınıyor…","ok");setTimeout(draw,1500)}})};
  draw();
}
/* ---- Monitör ---- */
async function monBind(){
  document.querySelectorAll("[data-wm]").forEach(b=>b.onclick=async()=>{try{const j=await(await post("/api/monitor",{action:"warm",level:+b.dataset.wm})).json();
    document.querySelectorAll("[data-wm]").forEach(x=>x.classList.toggle("on",+x.dataset.wm===j.level));
    if(j.level!==+b.dataset.wm)toast(j.level?"Ekran kartın bu kadar sıcak tona izin vermedi, en yakını uygulandı.":"Ekran kartın renk değişikliğine izin vermedi. Windows gece ışığı ayarını kullan.","err")}catch(e){toast("PC'ye ulaşılamadı.","err")}});
  $("#moff").onclick=async()=>{if(!confirm("Monitör kapatılsın mı? Fareyi oynatınca (touchpad) geri açılır."))return;await post("/api/monitor",{action:"off"});toast("Ekran kapatıldı.","ok")};
  $("#mnl").onclick=async()=>{await post("/api/monitor",{action:"nightsettings"});toast("PC'de gece ışığı ayarı açıldı.","ok")};
  let m;try{m=await(await fetch("/api/monitors",{headers:H()})).json()}catch(e){return}
  const el=$("#mons");if(!el)return;
  document.querySelectorAll("[data-wm]").forEach(x=>x.classList.toggle("on",+x.dataset.wm===(m.warm||0)));
  const ok=m.list.filter(x=>x.v!=null);
  el.innerHTML=ok.length?ok.map(x=>`<div class="bar" style="margin:0"><div><span>${esc(x.name)}</span><i id="bv${x.id}">%${x.v}</i></div></div><input type="range" min="0" max="100" step="5" value="${x.v}" data-br="${x.id}">`).join(""):`<p class="note" style="margin-bottom:8px">Parlaklık ayarlanamıyor. Harici monitörde, monitörün kendi menüsünden DDC/CI seçeneğini aç.</p>`;
  el.querySelectorAll("[data-br]").forEach(r=>{let t;r.oninput=()=>{$("#bv"+r.dataset.br).textContent="%"+r.value;clearTimeout(t);t=setTimeout(async()=>{const q=await post("/api/monitor",{action:"bright",id:r.dataset.br,v:+r.value});if(!q.ok)toast("Parlaklık değiştirilemedi.","err")},250)}});
}
/* ---- Görev yöneticisi ---- */
let tmAll=false;
function tmBind(){
  const go=async()=>{const el=$("#tm");if(!el)return;el.innerHTML=`<div class="empty" style="padding:14px 0">Ölçülüyor…</div>`;
    let L;try{const r=await fetch("/api/procs",{headers:H()});if(!r.ok)throw 0;L=await r.json()}catch(e){if($("#tm"))$("#tm").innerHTML=`<p class="note">Liste alınamadı.</p>`;return}
    if(!$("#tm"))return;
    const show=tmAll?L:L.slice(0,12);
    $("#tm").innerHTML=show.map(p=>`<div class="pr"><div><b>${esc(p.desc||p.name)}${p.n>1?` <span style="display:inline;font-weight:400">×${p.n}</span>`:""}</b><span>${esc(p.title||p.name+".exe")}</span></div><i class="${p.cpu>=25?"hot":""}">%${p.cpu}</i><i>${p.mem>=1024?(p.mem/1024).toFixed(1)+" GB":p.mem+" MB"}</i>${p.sys?`<button class="chip" disabled title="Sistem">Sistem</button>`:`<button class="chip" data-kp="${esc(p.name)}" style="color:var(--bad)">Kapat</button>`}</div>`).join("")+
      (L.length>12?`<button class="btn" style="width:100%;margin-top:10px" id="tma">${tmAll?"Daha az göster":"Tümünü göster ("+L.length+")"}</button>`:"");
    $("#tm").querySelectorAll("[data-kp]").forEach(b=>b.onclick=async()=>{if(!confirm(b.dataset.kp+" kapatılsın mı?\n\nKaydedilmemiş işler kaybolabilir."))return;
      const r=await post("/api/proc",{name:b.dataset.kp});toast(r.ok?b.dataset.kp+" kapatıldı.":"Bu program kapatılamaz.",r.ok?"ok":"err");if(r.ok)setTimeout(go,800)});
    const a=$("#tma");if(a)a.onclick=()=>{tmAll=!tmAll;go()}};
  $("#tmo").onclick=go;$("#tmr").onclick=go;
}
/* ---- İnterkom ---- */
let pttRec=null,micOn=null;
function pttBind(){
  const w=$("#pttw");if(!w)return;
  if(!window.isSecureContext||!navigator.mediaDevices){
    const u=`https://${location.hostname}:${(+location.port||80)+1}/`;
    w.innerHTML=`<p class="note" style="margin-bottom:8px">Telefon mikrofonu sadece güvenli bağlantıda çalışır. Aşağıdaki adresi aç; "bağlantı gizli değil" uyarısında <b>Ayrıntıları göster > Bu web sitesini ziyaret et</b> de, PIN'ini gir ve buradan konuş.</p><a class="btn" style="display:block;text-align:center;text-decoration:none" href="${u}">Güvenli bağlantıyı aç</a>`;
  }else{
    w.innerHTML=`<button class="ptt" id="ptt">Basılı tut ve konuş</button>`;
    const b=$("#ptt");
    const start=e=>{e.preventDefault();if(pttRec)return;b.classList.add("rec");b.textContent="Dinliyorum… bırakınca gönderilir";
      const pr=recStart();pttRec={p:pr,cancel(){pr.then(r=>r.cancel()).catch(()=>{});pttRec=null}};
      pr.catch(()=>{pttRec=null;b.classList.remove("rec");b.textContent="Basılı tut ve konuş";toast("Mikrofon izni verilmedi.","err")})};
    const stop=async e=>{e.preventDefault();if(!pttRec)return;const pr=pttRec.p;pttRec=null;b.classList.remove("rec");b.textContent="Basılı tut ve konuş";
      let r;try{r=await pr}catch(x){return}
      const wav=await r.stop();if(!wav){toast("Çok kısa, biraz daha uzun basılı tut.");return}
      try{const q=await fetch("/api/speak",{method:"POST",headers:{"X-Pin":pin,"Content-Type":"audio/wav"},body:wav});toast(q.ok?"PC'de çalıyor.":"Gönderilemedi.",q.ok?"ok":"err")}catch(x){toast("PC'ye ulaşılamadı.","err")}};
    b.addEventListener("touchstart",start,{passive:false});b.addEventListener("touchend",stop,{passive:false});b.addEventListener("touchcancel",stop,{passive:false});
    b.addEventListener("mousedown",start);b.addEventListener("mouseup",stop);b.addEventListener("mouseleave",e=>{if(pttRec)stop(e)});
  }
  $("#micl").onclick=micToggle;
}
async function recStart(){
  const st=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
  const AC=window.AudioContext||window.webkitAudioContext,ctx=new AC(),src=ctx.createMediaStreamSource(st),pr=ctx.createScriptProcessor(4096,1,1),bufs=[];let len=0,done=false;
  pr.onaudioprocess=e=>{if(done)return;const d=e.inputBuffer.getChannelData(0);bufs.push(new Float32Array(d));len+=d.length;if(len>ctx.sampleRate*60)done=true};
  src.connect(pr);pr.connect(ctx.destination);
  const end=()=>{done=true;try{pr.disconnect();src.disconnect()}catch(e){}st.getTracks().forEach(t=>t.stop());try{ctx.close()}catch(e){}};
  return{cancel:end,stop:async()=>{const rate=ctx.sampleRate;end();if(len<rate*0.3)return null;
    const all=new Float32Array(len);let o=0;for(const b of bufs){all.set(b,o);o+=b.length}
    const R=16000,n=Math.floor(len*R/rate),pcm=new Int16Array(n);
    for(let i=0;i<n;i++){const v=Math.max(-1,Math.min(1,all[Math.floor(i*rate/R)]*1.6));pcm[i]=v<0?v*32768:v*32767}
    const h=new DataView(new ArrayBuffer(44)),ws=(o,t)=>{for(let i=0;i<t.length;i++)h.setUint8(o+i,t.charCodeAt(i))};
    ws(0,"RIFF");h.setUint32(4,36+n*2,true);ws(8,"WAVE");ws(12,"fmt ");h.setUint32(16,16,true);h.setUint16(20,1,true);h.setUint16(22,1,true);
    h.setUint32(24,R,true);h.setUint32(28,R*2,true);h.setUint16(32,2,true);h.setUint16(34,16,true);ws(36,"data");h.setUint32(40,n*2,true);
    return new Blob([h.buffer,pcm.buffer],{type:"audio/wav"})}};
}
async function micToggle(){
  const b=$("#micl");
  if(micOn){micOn.stop();return}
  const AC=window.AudioContext||window.webkitAudioContext;if(!AC||!window.ReadableStream){toast("Bu tarayıcı desteklemiyor.","err");return}
  const ctx=new AC();try{ctx.resume()}catch(e){}
  let r;try{r=await fetch("/api/mic",{headers:H()})}catch(e){ctx.close();toast("PC'ye ulaşılamadı.","err");return}
  if(r.status===501){ctx.close();if(confirm("Bunun için PC'ye küçük bir ses bileşeni (PyAudioWPatch) kurulmalı. Kurulsun mu?")){await post("/api/audio-install",{});toast("Kuruluyor… 1–2 dakika sonra tekrar dene.","ok")}return}
  if(!r.ok||!r.body){ctx.close();toast("PC mikrofonu açılamadı.","err");return}
  const rate=+r.headers.get("X-Rate")||24000,rd=r.body.getReader();let t=0,rest=null,on=true;
  micOn={stop(){on=false;try{rd.cancel()}catch(e){}try{ctx.close()}catch(e){}micOn=null;const x=$("#micl");if(x){x.classList.remove("on");x.textContent="PC mikrofonunu dinle"}}};
  b.classList.add("on");b.textContent="Dinlemeyi durdur";
  while(on){let x;try{x=await rd.read()}catch(e){break}if(x.done)break;
    let u=x.value;if(rest){const m=new Uint8Array(rest.length+u.length);m.set(rest);m.set(u,rest.length);u=m;rest=null}
    if(u.length%2){rest=u.slice(-1);u=u.slice(0,-1)}
    const pcm=new Int16Array(u.slice().buffer),n=pcm.length;if(!n||!on)continue;
    const buf=ctx.createBuffer(1,n,rate),c=buf.getChannelData(0);for(let i=0;i<n;i++)c[i]=pcm[i]/32768;
    const now=ctx.currentTime;if(t<now+0.04||t>now+0.7)t=now+0.12;
    const s=ctx.createBufferSource();s.buffer=buf;s.connect(ctx.destination);s.start(t);t+=buf.duration}
  if(micOn&&on)micOn.stop();
}
/* ---- İstek listesi ---- */
async function openDiscover(){
  const d=bigSheet("🔍 Keşfet"),b=d.querySelector(".sb");
  let D;try{D=await(await fetch("/api/discover",{headers:H()})).json()}catch(e){b.innerHTML=`<p class="note">Alınamadı.</p>`;return}
  if(!b.isConnected)return;
  let h=`<h2 style="margin:0 0 4px">Arkadaşlarından öneriler</h2><p class="note" style="margin:0 0 10px;font-size:12px">Arkadaşlarının oynadığı ama sende olmayan oyunlar.</p>`;
  h+=D.rec&&D.rec.length?D.rec.map(r=>`<div class="top"><img alt="" src="${img(r.appid)}"><div><div class="tn"><span>${esc(r.name||("Oyun "+r.appid))}</span></div><span class="note">${r.n} arkadaşında var: ${esc(r.who.slice(0,3).join(", "))}</span></div><button class="chip" data-di="${r.appid}" data-dn="${esc(r.name||"")}">Kur</button></div>`).join(""):`<p class="note">Henüz öneri yok. Arkadaşların oyun oynadıkça burası dolar.</p>`;
  h+=`<h2 style="margin:16px 0 4px">Birlikte oynama</h2><p class="note" style="margin:0 0 10px;font-size:12px">Arkadaşlarınla aynı anda oyunda geçirdiğin süre.</p>`;
  h+=D.coplay&&D.coplay.length?D.coplay.map((c,i)=>`<div class="vis">${cav(c.name)}<div><b>${["🥇","🥈","🥉"][i]||""} ${esc(c.name)}</b><span>${fmh(c.total)} birlikte${c.same?" · "+fmh(c.same)+" aynı oyunda":""}</span></div></div>`).join(""):`<p class="note">Henüz kayıt yok. Bir arkadaşınla aynı anda oyundayken süre birikir.</p>`;
  b.innerHTML=h;
  b.querySelectorAll("[data-di]").forEach(x=>x.onclick=()=>{d.remove();steamApp(x.dataset.di,"install",x.dataset.dn)});
}
const wlRow=g=>`<div class="wl"><img alt="" src="${img(g.appid)}"><div><b>${esc(g.name)}</b><div class="pp">${g.pct?`<span class="disc">-%${g.pct}</span>${g.orig?`<s>${esc(g.orig)}</s>`:""}`:""}${g.free?"Ücretsiz":esc(g.price||"Fiyat yok")}</div></div><button class="chip" data-ws="${g.appid}">Mağaza</button></div>`;
async function openWl(force){
  const d=bigSheet("İstek listesi"),b=d.querySelector(".sb");
  let L;try{L=await(await fetch("/api/wishlist"+(force?"?force=1":""),{headers:H()})).json()}catch(e){L={error:"net"}}
  if(!b.isConnected)return;
  if(L.error){b.innerHTML=`<p class="note">${L.error==="key"?"İstek listen için Ayarlar'a Steam API anahtarını ekle.":"İstek listesi alınamadı. Steam profilinin ve istek listenin herkese açık olduğundan emin ol."}</p>`;return}
  const it=L.items,sale=it.filter(g=>g.pct);let q="";
  const draw=()=>{const f=it.filter(g=>g.name.toLowerCase().includes(q));
    b.querySelector("#wll").innerHTML=f.length?f.map(wlRow).join(""):`<p class="note">${it.length?"Eşleşen oyun yok.":"İstek listen boş."}</p>`;
    b.querySelectorAll("[data-ws]").forEach(x=>x.onclick=()=>wlStore(x.dataset.ws))};
  b.innerHTML=`<p class="note" style="margin-bottom:10px">${it.length} oyun · ${sale.length?`<b style="color:var(--ok)">${sale.length} tanesi indirimde</b>`:"şu an indirimde olan yok"}. İndirime giren olursa telefona bildirim gelir (Ayarlar > Telefona bildirimler).</p>
    <div class="row" style="margin:0 0 10px"><input type="search" id="wlq" placeholder="Ara" style="flex:1;width:auto;padding:10px 12px;font-size:15px"><button class="chip" id="wlr">Yenile</button></div><div id="wll"></div>`;
  $("#wlq").oninput=e=>{q=e.target.value.toLowerCase().trim();draw()};$("#wlr").onclick=()=>{d.remove();openWl(true)};draw();
}
function wlStore(id){
  if(confirm("Mağaza sayfası PC'de Steam'de açılsın mı?\n\n(İptal'e basarsan telefonda açılır.)")){post("/api/storeopen",{appid:id});toast("PC'de açılıyor.","ok")}
  else window.open("https://store.steampowered.com/app/"+id,"_blank");
}
/* ---- Ekran görüntüleri ---- */
let shotsD=null,shotSrc="all",shotN=24;
async function loadShots(){
  try{shotsD=await(await fetch("/api/shots",{headers:H()})).json()}catch(e){if($("#shg"))$("#shg").innerHTML=`<p class="note">Alınamadı.</p>`;return}
  drawShots();
}
function drawShots(){
  const g=$("#shg");if(!g||!shotsD)return;
  const srcs=[...new Set(shotsD.list.map(x=>x.src))],L=shotsD.list.filter(x=>shotSrc==="all"||x.src===shotSrc);
  $("#shf").innerHTML=srcs.length>1?[["all","Tümü"],...srcs.map(x=>[x,x])].map(([k,l])=>`<button class="chip${shotSrc===k?" on":""}" data-ss="${esc(k)}">${esc(l)}</button>`).join(""):"";
  $("#shf").querySelectorAll("[data-ss]").forEach(b=>b.onclick=()=>{shotSrc=b.dataset.ss;shotN=24;drawShots()});
  if(!L.length){g.innerHTML=`<p class="note">Ekran görüntüsü bulunamadı. Steam'de oyun içinde F12 ile, Windows'ta Win+PrtSc ile alınanlar burada görünür.</p>`;return}
  const u=(x,k)=>`/shot?t=${shotsD.tok}&id=${x.id}${k}`;
  g.innerHTML=`<div class="sg">${L.slice(0,shotN).map(x=>`<button data-sv="${x.id}"><img loading="lazy" alt="" src="${u(x,"&th=1")}"><span>${esc(x.name||x.src)} · ${new Date(x.t*1e3).toLocaleDateString("tr-TR",{day:"numeric",month:"short"})}</span></button>`).join("")}</div>`+
    (L.length>shotN?`<button class="btn" style="width:100%;margin-top:10px" id="shm">Daha fazla (${L.length-shotN})</button>`:"");
  g.querySelectorAll("[data-sv]").forEach(b=>b.onclick=()=>{const x=shotsD.list[+b.dataset.sv];const v=document.createElement("div");v.id="viewer";
    v.innerHTML=`<img alt="" src="${u(x,"")}"><div class="vb"><span style="flex:1">${esc(x.name||x.src)} · ${new Date(x.t*1e3).toLocaleString("tr-TR",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})} · ${fsz(x.size)}<br><span style="font-size:11px;opacity:.7">Fotoğraflara kaydetmek için resme basılı tut.</span></span><a class="chip" href="${u(x,"&dl=1")}">İndir</a><button class="chip" id="vsh">Sohbette paylaş</button><button class="chip on" id="vx">Kapat</button></div>`;
    document.body.appendChild(v);$("#vx").onclick=()=>v.remove();$("#vsh").onclick=()=>shareToChat(u(x,""))});
  const m=$("#shm");if(m)m.onclick=()=>{shotN+=24;drawShots()};
}
/* ---- Oyun modu ---- */
async function loadGm(){
  let c;try{c=await(await fetch("/api/config-gm",{headers:H()})).json()}catch(e){return}
  const el=$("#gmc");if(!el)return;const g=c.gm;
  el.innerHTML=`<div class="row" style="margin:0 0 10px"><h2 style="margin:0">Oyun modu</h2><span class="sp"></span><button class="chip${g.on?" on":""}" id="gmon">${g.on?"Açık":"Kapalı"}</button></div>
  <p class="note" style="margin-bottom:10px">Bir oyun açılınca aşağıdakiler kendiliğinden yapılır, oyun kapanınca güç planı eski haline döner.</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 12px"><button class="chip${g.power?" on":""}" id="gmpw">Yüksek performans güç planı</button></div>
  <p class="note" style="margin-bottom:6px">Oyun açılınca şunları aç (Programlar kartından):</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 12px">${c.apps.length?c.apps.map(n=>`<button class="chip${g.open.includes(n)?" on":""}" data-go="${esc(n)}">${esc(n)}</button>`).join(""):`<span class="note">Önce aşağıdaki Programlar kartına program ekle.</span>`}</div>
  <p class="note" style="margin-bottom:6px">Oyun açılınca şunları kapat (virgülle ayır):</p>
  <input type="text" id="gmcl" value="${esc(g.close)}" placeholder="örn. chrome, OneDrive, Teams" autocomplete="off" autocapitalize="off"><button class="btn" style="width:100%" id="gms">Kaydet</button>`;
  const sv=o=>post("/api/gm",o).then(r=>toast(r.ok?"Kaydedildi.":"Kaydedilemedi.",r.ok?"ok":"err")).catch(()=>toast("PC'ye ulaşılamadı.","err"));
  $("#gmon").onclick=e=>{g.on=!g.on;e.currentTarget.classList.toggle("on",g.on);e.currentTarget.textContent=g.on?"Açık":"Kapalı";sv({on:g.on})};
  $("#gmpw").onclick=e=>{g.power=!g.power;e.currentTarget.classList.toggle("on",g.power);sv({power:g.power})};
  el.querySelectorAll("[data-go]").forEach(b=>b.onclick=()=>{const n=b.dataset.go;g.open=g.open.includes(n)?g.open.filter(x=>x!==n):[...g.open,n];b.classList.toggle("on",g.open.includes(n));sv({open:g.open})});
  $("#gms").onclick=()=>sv({close:$("#gmcl").value});
}
/* ---- Özet ---- */
let homeFr=null,homeFrT=0,homeAch={};
function greet(){const h=new Date().getHours();return h<6?"İyi geceler":h<12?"Günaydın":h<18?"İyi günler":"İyi akşamlar"}
const fmh=m=>m>=60?Math.floor(m/60)+" sa "+(m%60?m%60+" dk":""):m+" dk";
async function homeView(){
  $("#main").innerHTML=`<header><div class="row" style="margin:0 0 10px"><h1 id="hg" style="margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${greet()}</h1><button class="chip" id="hprof">Profil</button></div></header><div id="hm"><div class="empty">Yükleniyor…</div></div>`;
  $("#hprof").onclick=()=>{tab="profile";show()};
  await homeDraw(true);sysT=setInterval(()=>{if(!document.hidden)homeDraw()},4000);
}
async function homeDraw(first){
  let d;try{const r=await fetch("/api/home",{headers:H()});if(!r.ok)throw 0;d=await r.json()}catch(e){if(first&&$("#hm"))$("#hm").innerHTML=`<div class="empty">PC'ye ulaşılamadı.</div>`;return}
  if(tab!=="home"||!$("#hm"))return;
  $("#hg").textContent=greet()+(d.name?", "+d.name:"");
  running.appid=d.appid;running.name=d.game;since=Date.now()-d.elapsed*1e3;
  const s=d.sys,playing=d.appid!=="0",tile=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  let pre="";const dis=(()=>{try{return JSON.parse(localStorage.getItem("annx")||"[]")}catch(e){return[]}})();
  (d.anns||[]).filter(a=>!dis.includes(a.id)&&Date.now()/1e3-a.ts<7*86400).slice(-2).reverse().forEach(a=>{pre+=`<div class="ann"><div class="row" style="margin:0"><b style="flex:1">📢 ${esc(a.title)}</b><button class="chip" data-ax="${a.id}">✕</button></div>${a.text?`<p>${esc(a.text)}</p>`:""}</div>`});
  let pf=null;if(playing){try{pf=(await(await fetch("/api/perf",{headers:H()})).json()).now}catch(e){}}
  if(playing&&!homeAch[d.appid]){homeAch[d.appid]={};fetch("/api/ach?appid="+d.appid,{headers:H()}).then(r=>r.json()).then(a=>{homeAch[d.appid]=a}).catch(()=>{})}
  if(Date.now()-homeFrT>60000){homeFrT=Date.now();fetch("/api/friends",{headers:H()}).then(r=>r.json()).then(f=>{homeFr=f}).catch(()=>{})}
  let h="";
  if(playing){
    h+=`<div class="hero" style="margin-top:0"><img alt="" src="${img(d.appid)}"><div class="cap"><small style="color:var(--ok)">Şimdi oynanıyor · ${fmt(d.elapsed)}</small><b>${esc(d.game)}</b></div></div>`;
    if(pf){const t=(v,u,l,w)=>v==null?"":`<div class="pt"><span>${l}</span><b${w&&v>=w?' style="color:var(--warn)"':""}>${v}${u}</b></div>`;
      const pt=t(pf.fps,"","FPS")+t(pf.gt,"°C","Ekran kartı",80)+t(pf.ct,"°C","İşlemci",80)+t(pf.gu,"%","GPU yükü");if(pt)h+=`<div class="card" style="margin:10px 0 0"><div class="pf">${pt}</div></div>`}
    h+=`<div class="btns" style="margin:10px 0 12px"><button class="btn bad" id="hstop">Oyunu kapat</button><button class="btn" id="hscr">Ekranı gör</button></div>`;
    const a=homeAch[d.appid];
    if(a&&a.total)h+=`<div class="card"><div class="bar" style="margin:0"><div><span>Başarımlar</span><i>${a.done} / ${a.total}</i></div><p><b style="width:${a.done/a.total*100}%"></b></p></div><button class="btn" style="width:100%;margin-top:12px" id="hach">Başarımları gör</button></div>`;
  }else{
    const top=games.find(g=>g.last>0&&!g.hidden);
    if(top)h+=`<button class="hero" style="margin-top:0;margin-bottom:12px" id="hcont" data-id="${top.appid}"><img alt="" src="${img(top.appid)}"><div class="cap"><small>Kaldığın yerden devam et</small><b>${esc(top.name)}</b></div><span class="go">Oyna</span></button>`;
  }
  h+=`<div class="card"><h2>Bilgisayar</h2><div class="sts">${tile(s.cpu+"%","İşlemci")+tile(s.ram+"%","Bellek")+(s.gpu?tile(s.gpu[1]+"°C","Ekran kartı sıcaklığı")+tile(s.gpu[0]+"%","Ekran kartı yükü"):"")}</div><p class="note" style="margin-top:10px">${upt(s.uptime)} önce açıldı${d.files?` · Aktarım klasöründe ${d.files} dosya var`:""}</p></div>`;
  h+=`<div class="card"><h2>Oyun süresi</h2><div class="sts">${tile(fmh(d.today),"Bugün")+tile(fmh(d.week),"Son 7 gün")}</div>${d.goal?`<div class="bar" style="margin:12px 0 0"><div><span>Günlük hedef</span><i>${d.today} / ${d.goal} dk</i></div><p><b style="width:${Math.min(100,d.today/d.goal*100)}%${d.today>=d.goal?";background:var(--warn)":""}"></b></p></div>`:""}${d.streak>1?`<p class="note" style="margin-top:10px">${d.streak} gündür üst üste oynuyorsun.</p>`:""}</div>`;
  const dl=s.dl||[];
  if(dl.length||d.updates.length)h+=`<div class="card"><h2>İndirmeler</h2>${dl.map(x=>`<div class="bar"><div><span>${esc(x.name)}</span><i>${x.pct}%</i></div><p><b style="width:${x.pct}%"></b></p></div>`).join("")}${d.updates.length?`<p class="note" style="margin-top:${dl.length?10:0}px">Güncelleme bekliyor: ${d.updates.map(u=>esc(u.name)).join(", ")}</p>`:""}</div>`;
  if(d.sales&&d.sales.length)h+=`<div class="card"><h2>İstek listende indirimde · ${d.sales.length}</h2>${d.sales.slice(0,4).map(wlRow).join("")}<button class="btn" style="width:100%" id="hwl">Tüm istek listesi</button></div>`;
  const fr=homeFr&&homeFr.friends?homeFr.friends.filter(x=>x.st>0||x.game):null;
  if(fr)h+=`<div class="card"><h2>Çevrimiçi arkadaşlar · ${fr.length}</h2>${fr.length?`<div class="strip">${fr.slice(0,15).map(x=>`<button class="fmini" data-gofr="1">${x.av?`<img class="fa" alt="" src="${esc(x.av)}">`:`<div class="fa">${esc((x.name||"?")[0].toUpperCase())}</div>`}<b>${esc(x.name)}</b><span class="${x.game?"g":"o"}">${esc(x.game||PST[x.st]||"Çevrimiçi")}</span></button>`).join("")}</div>`:`<p class="note">Şu an kimse çevrimiçi değil.</p>`}</div>`;
  h+=`<div class="card"><h2>Hızlı işlemler</h2><div class="btns"><button class="btn" id="hrnd">Rastgele oyun</button><button class="btn" data-hs="bigpicture">Big Picture</button><button class="btn" data-hv="down">Ses −</button><button class="btn" data-hv="up">Ses +</button><button class="btn" data-hv="playpause">Oynat / Duraklat</button><button class="btn" id="hlib">Kur / Güncelle</button></div></div>`;
  const fn=(d.fnow||[]);
  if(!ME.standalone&&fn.length)h+=`<div class="card"><h2>Arkadaşların · ${fn.filter(f=>f.game).length} oyunda</h2>${fn.slice(0,8).map(f=>`<div class="cv" data-fd="${f.code}">${f.prof?avatarHTML(f.prof,40):cav(f.name)}<div><b>${esc(f.name)}</b>${f.game?`<span class="g">🎮 ${esc(f.game.name)}${f.mins!=null?" · "+fmh(f.mins):""}</span>`:`<span>${f.seen?"Son görülme: "+tm2(f.seen):"Çevrimdışı"}</span>`}</div>${f.game&&games.some(g=>g.appid===f.game.appid)?`<button class="chip" data-fj="${f.game.appid}">Katıl</button>`:""}</div>`).join("")}</div>`;
  const ev=(d.events||[]);
  if(ev.length)h+=`<div class="card"><h2>Yaklaşan oyun geceleri</h2>${ev.slice(0,4).map(e=>`<div class="cv" data-fe="${e.cid}">${e.game?`<img alt="" src="${img(e.game.appid)}" style="width:72px;border-radius:8px;aspect-ratio:460/215;object-fit:cover;flex:none">`:`<div class="cav room">📅</div>`}<div><b>${esc(e.title)}</b><span>${new Date(e.when*1e3).toLocaleString("tr-TR",{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})} · ${esc(e.conv)} · ${e.yes} katılıyor</span></div><i>${e.mine==="yes"?"✅":e.mine==="maybe"?"🤔":e.mine==="no"?"❌":""}</i></div>`).join("")}</div>`;
  $("#hm").innerHTML=pre+h;
  document.querySelectorAll("[data-ax]").forEach(b=>b.onclick=()=>{dis.push(b.dataset.ax);try{localStorage.setItem("annx",JSON.stringify(dis.slice(-50)))}catch(e){}b.closest(".ann").remove()});
  document.querySelectorAll("[data-fd]").forEach(b=>b.onclick=e=>{if(e.target.closest("[data-fj]"))return;tab="chat";show();setTimeout(()=>openConv("d:"+b.dataset.fd),300)});
  document.querySelectorAll("[data-fj]").forEach(b=>b.onclick=()=>{const g=games.find(x=>x.appid===b.dataset.fj);if(g)launch(g.appid,g.name,null)});
  document.querySelectorAll("[data-fe]").forEach(b=>b.onclick=()=>{tab="chat";show();setTimeout(()=>openConv(b.dataset.fe),300)});
  const on=(sel,f)=>{const e=$(sel);if(e)e.onclick=f};
  on("#hstop",stopGame);on("#hscr",()=>{tab="pc";show();setTimeout(()=>{const b=$("#live");if(b)b.click()},300)});
  on("#hach",()=>openAch(d.appid,d.game));on("#hwl",()=>openWl());document.querySelectorAll("#hm [data-ws]").forEach(b=>b.onclick=()=>wlStore(b.dataset.ws));on("#hrnd",rand);on("#hlib",openLib);
  on("#hcont",e=>{const g=games.find(x=>x.appid===e.currentTarget.dataset.id);if(g)launch(g.appid,g.name,e.currentTarget)});
  document.querySelectorAll("[data-hv]").forEach(b=>b.onclick=()=>post("/api/volume",{action:b.dataset.hv}));
  document.querySelectorAll("[data-hs]").forEach(b=>b.onclick=async()=>{await post("/api/steam",{action:b.dataset.hs});toast("Big Picture açılıyor.","ok")});
  document.querySelectorAll("[data-gofr]").forEach(b=>b.onclick=()=>{tab="chat";show()});
}
/* ---- Başarımlar ---- */
function bigSheet(title){
  const d=document.createElement("div");d.id="sheet";
  d.innerHTML=`<div class="sh"><div class="row" style="margin:0 0 12px"><h2 style="margin:0;flex:1">${title}</h2><button class="chip" data-close>Kapat</button></div><div class="sb"><div class="empty">Yükleniyor…</div></div></div>`;
  document.body.appendChild(d);d.onclick=e=>{if(e.target===d)d.remove()};d.querySelector("[data-close]").onclick=()=>d.remove();
  return d;
}
async function openAch(id,name){
  const d=bigSheet(esc(name||"Oyun")+" · Başarımlar"),b=d.querySelector(".sb");
  let a;try{a=await(await fetch("/api/ach?appid="+id,{headers:H()})).json()}catch(e){a={error:"net"}}
  if(!b.isConnected)return;
  if(a.error){b.innerHTML=`<p class="note">${a.error==="key"?"Başarımları görmek için Ayarlar'daki Steam API anahtarı kartına anahtarını ekle.":a.error==="none"?"Bu oyunun başarımı yok ya da Steam gizlilik ayarlarında \"Oyun ayrıntıları\" gizli.":"Başarımlar şu an alınamadı."}</p>`;return}
  const done=a.list.filter(x=>x.done).sort((p,q)=>q.t-p.t),left=a.list.filter(x=>!x.done).sort((p,q)=>(q.pct||0)-(p.pct||0));
  const row=x=>`<div class="ach${x.done?"":" lk"}">${x.icon?`<img alt="" src="${esc(x.icon)}">`:`<img alt="">`}<div><b>${esc(x.name)}</b><span>${esc(x.desc)}</span></div><i>${x.done&&x.t?new Date(x.t*1e3).toLocaleDateString("tr-TR",{day:"numeric",month:"short",year:"2-digit"}):x.pct!=null?"%"+x.pct:""}</i></div>`;
  let mode=done.length?"recent":"easy";
  const draw=()=>{b.innerHTML=`<div class="bar"><div><span>${a.done} / ${a.total} açıldı</span><i>%${Math.round(a.done/a.total*100)}</i></div><p><b style="width:${a.done/a.total*100}%"></b></p></div>
    <div class="row" style="margin:12px 0;flex-wrap:wrap">${[["recent","Son açılanlar"],["easy","Kolay kalanlar"],["all","Tümü"]].map(([k,l])=>`<button class="chip${mode===k?" on":""}" data-am="${k}">${l}</button>`).join("")}</div>`+
    (mode==="easy"?`<p class="note" style="margin:0 0 10px;font-size:12px">Sağdaki oran: o başarımı açan oyuncuların yüzdesi. Yüksek olan daha kolaydır.</p>`:"")+
    (mode==="recent"?(done.length?done.slice(0,40).map(row).join(""):`<p class="note">Henüz açılan başarım yok.</p>`):mode==="easy"?(left.length?left.map(row).join(""):`<p class="note">Hepsini açmışsın, tebrikler!</p>`):[...done,...left].map(row).join(""));
    b.querySelectorAll("[data-am]").forEach(x=>x.onclick=()=>{mode=x.dataset.am;draw()})};
  draw();
}
/* ---- Kütüphane ---- */
async function steamApp(id,act,name){
  const M={install:`${name} kurulsun mu?\n\nPC'de Steam'in kurulum penceresi açılır, orada "Yükle"ye basman gerekir (PC sekmesindeki canlı ekrandan da basabilirsin).`,
    uninstall:`${name} PC'den kaldırılsın mı?\n\nPC'de Steam onay penceresi açılır, orada "Kaldır"a basman gerekir.`,
    validate:`${name} oyun dosyaları doğrulansın mı? Bozuk dosyalar yeniden indirilir.`};
  if(M[act]&&!confirm(M[act]))return;
  try{const r=await post("/api/steamapp",{appid:id,action:act});toast(r.ok?(act==="update"?"PC'de İndirmeler sayfası açıldı.":"PC'de Steam penceresi açıldı."):"Yapılamadı.",r.ok?"ok":"err")}catch(e){toast("PC'ye ulaşılamadı.","err")}
}
async function openLib(){
  const d=bigSheet("Kur / Güncelle"),b=d.querySelector(".sb");
  let L;try{L=await(await fetch("/api/library",{headers:H()})).json()}catch(e){b.innerHTML=`<p class="note">Bilgi alınamadı.</p>`;return}
  if(!b.isConnected)return;
  const gb=x=>(x/2**30).toFixed(0)+" GB";
  let h=`<p class="note" style="margin-bottom:8px">Disk alanı</p>`+L.disks.map(k=>`<div class="bar"><div><span>${esc(k.drive)}</span><i>${gb(k.free)} boş / ${gb(k.total)}</i></div><p><b style="width:${(1-k.free/k.total)*100}%${k.free/k.total<.1?";background:var(--bad)":""}"></b></p></div>`).join("");
  h+=`<p class="note" style="margin:16px 0 8px">Güncelleme bekleyenler</p>`+(L.updates.length?L.updates.map(u=>`<div class="top"><img alt="" src="${img(u.appid)}"><div><div class="tn"><span>${esc(u.name)}</span></div></div><button class="chip" data-up="${u.appid}">Güncelle</button></div>`).join(""):`<p class="note">Tüm oyunlar güncel.</p>`);
  h+=`<p class="note" style="margin:16px 0 8px">Kütüphanendeki kurulu olmayan oyunlar</p>`;
  if(L.notinst==null)h+=`<p class="note">${L.has_key?"Steam kütüphanen alınamadı. Gizlilik ayarlarında \"Oyun ayrıntıları\" herkese açık olmalı.":"Bu listeyi görmek için Ayarlar'a Steam API anahtarını ekle."}</p>`;
  else h+=`<input type="text" id="lq" placeholder="Oyun ara (${L.notinst.length} oyun)" autocomplete="off" style="width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:10px;outline:none"><div id="ll"></div>`;
  b.innerHTML=h;
  b.querySelectorAll("[data-up]").forEach(x=>x.onclick=()=>steamApp(x.dataset.up,"update"));
  if(L.notinst){const draw=()=>{const q=($("#lq").value||"").toLowerCase().trim(),a=L.notinst.filter(g=>g.name.toLowerCase().includes(q)).slice(0,60);
      $("#ll").innerHTML=a.length?a.map(g=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${esc(g.name)}</span></div><span class="note">${g.hours?g.hours+" sa oynandı":"Hiç oynanmadı"}</span></div><button class="chip" data-in="${g.appid}" data-n="${esc(g.name)}">Kur</button></div>`).join(""):`<p class="note">Eşleşen oyun yok.</p>`;
      $("#ll").querySelectorAll("[data-in]").forEach(x=>x.onclick=()=>steamApp(x.dataset.in,"install",x.dataset.n))};
    $("#lq").oninput=draw;draw()}
}
/* ---- Touchpad ---- */
let tpAcc={dx:0,dy:0,sc:0},tpBusy=false,tpHeld=false;
function tpFlush(){
  if(tpBusy)return;const dx=Math.trunc(tpAcc.dx),dy=Math.trunc(tpAcc.dy),sc=Math.trunc(tpAcc.sc);if(!dx&&!dy&&!sc)return;
  tpAcc.dx-=dx;tpAcc.dy-=dy;tpAcc.sc-=sc;tpBusy=true;
  post("/api/mouse",{dx,dy,scroll:sc}).catch(()=>{}).finally(()=>{tpBusy=false;tpFlush()});
}
function mouseUp(){tpHeld=false;post("/api/mouse",{btn:"left",act:"up"}).catch(()=>{});const b=$("#tpd");if(b)b.classList.remove("on")}
function tpBind(){
  const el=$("#tp");if(!el)return;
  let last=null,t0=0,moved=0,maxN=0;
  const avg=ts=>{let x=0,y=0;for(const t of ts){x+=t.clientX;y+=t.clientY}return{x:x/ts.length,y:y/ts.length}};
  el.addEventListener("touchstart",e=>{e.preventDefault();if(!last){t0=Date.now();moved=0;maxN=0}maxN=Math.max(maxN,e.touches.length);last=avg(e.touches);el.classList.add("act")},{passive:false});
  el.addEventListener("touchmove",e=>{e.preventDefault();if(!last)return;const p=avg(e.touches),dx=p.x-last.x,dy=p.y-last.y;last=p;moved+=Math.abs(dx)+Math.abs(dy);
    const k=+pref("tps","2");
    if(e.touches.length>=2)tpAcc.sc+=-dy*5;else{const a=k*(1+Math.min(Math.hypot(dx,dy)/12,1.8));tpAcc.dx+=dx*a;tpAcc.dy+=dy*a}
    tpFlush()},{passive:false});
  const end=e=>{e.preventDefault();if(e.touches.length){last=avg(e.touches);return}
    if(moved<10&&Date.now()-t0<300)post("/api/mouse",{btn:maxN>=2?"right":"left"}).catch(()=>{});
    last=null;el.classList.remove("act")};
  el.addEventListener("touchend",end,{passive:false});el.addEventListener("touchcancel",end,{passive:false});
  document.querySelectorAll("[data-tps]").forEach(b=>b.onclick=()=>{setLocal("tps",b.dataset.tps);document.querySelectorAll("[data-tps]").forEach(x=>x.classList.toggle("on",x===b))});
  document.querySelectorAll("[data-mb]").forEach(b=>b.onclick=()=>post("/api/mouse",{btn:b.dataset.mb}));
  $("#tpd").onclick=e=>{if(tpHeld){mouseUp();return}tpHeld=true;e.currentTarget.classList.add("on");post("/api/mouse",{btn:"left",act:"down"});toast("Sol tık basılı. Touchpad ile sürükle, bitince tekrar bas.")};
  const send=async()=>{const t=$("#tpk").value;if(!t)return;await post("/api/type",{text:t});$("#tpk").value=""};
  $("#tpks").onclick=send;$("#tpk").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();send()}};
  document.querySelectorAll("[data-tk]").forEach(b=>b.onclick=()=>post("/api/key",{key:b.dataset.tk}));
}
/* ---- PC sesi ---- */
let aud=null;
async function audioToggle(){
  if(aud){aud.stop();return}
  const AC=window.AudioContext||window.webkitAudioContext;if(!AC||!window.ReadableStream){toast("Bu tarayıcı ses aktarımını desteklemiyor.","err");return}
  const ctx=new AC();try{ctx.resume()}catch(e){}
  let r;try{r=await fetch("/api/audio",{headers:H()})}catch(e){ctx.close();toast("PC'ye ulaşılamadı.","err");return}
  if(r.status===501){ctx.close();let j={};try{j=await r.json()}catch(e){}
    if(j.busy){toast("Ses bileşeni kuruluyor, 1 dakika sonra tekrar dene.");return}
    if(j.msg&&j.msg.startsWith("Kurulamadı")){toast(j.msg,"err");return}
    if(j.msg==="Kuruldu."){toast("Bileşen kuruldu ama yüklenemedi. Sunucuyu yeniden başlat.","err");return}
    if(confirm("PC sesini telefona aktarmak için PC'ye küçük bir ses bileşeni (PyAudioWPatch) kurulması gerekiyor. Şimdi kurulsun mu?")){await post("/api/audio-install",{});toast("Kuruluyor… 1–2 dakika sonra tekrar Ses'e bas.","ok")}
    return}
  if(!r.ok||!r.body){ctx.close();toast("PC sesi alınamadı.","err");return}
  const rate=+r.headers.get("X-Rate")||24000,rd=r.body.getReader();let t=0,rest=null,on=true;
  aud={stop(){on=false;try{rd.cancel()}catch(e){}try{ctx.close()}catch(e){}aud=null;const b=$("#aud");if(b)b.classList.remove("on")}};
  $("#aud").classList.add("on");toast("PC sesi açık. Telefon sessizde ise duyulmayabilir.","ok");
  while(on){let x;try{x=await rd.read()}catch(e){break}if(x.done)break;
    let u=x.value;if(rest){const m=new Uint8Array(rest.length+u.length);m.set(rest);m.set(u,rest.length);u=m;rest=null}
    if(u.length%2){rest=u.slice(-1);u=u.slice(0,-1)}
    const pcm=new Int16Array(u.slice().buffer),n=pcm.length;if(!n||!on)continue;
    const buf=ctx.createBuffer(1,n,rate),c=buf.getChannelData(0);for(let i=0;i<n;i++)c[i]=pcm[i]/32768;
    const now=ctx.currentTime;if(t<now+0.04||t>now+0.7)t=now+0.12;
    const src=ctx.createBufferSource();src.buffer=buf;src.connect(ctx.destination);src.start(t);t+=buf.duration;
  }
  if(aud&&on)aud.stop();
}
/* ---- Aylık rapor ---- */
let repM=null;
const MN=["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"];
async function loadReport(dir){
  if(!repM){const n=new Date();repM=[n.getFullYear(),n.getMonth()+1]}
  if(dir){let[y,m]=repM;m+=dir;if(m<1){m=12;y--}if(m>12){m=1;y++}repM=[y,m]}
  let r;try{r=await(await fetch(`/api/report?m=${repM[0]}-${repM[1]}`,{headers:H()})).json()}catch(e){return}
  const el=$("#rep");if(!el)return;
  const n=new Date(),cur=r.y===n.getFullYear()&&r.m===n.getMonth()+1,first=new Date(r.first*1e3),canPrev=first<new Date(r.y,r.m-1,1);
  const tile=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  let h=`<div class="row" style="margin:0 0 12px"><button class="chip" id="rpp"${canPrev?"":" disabled"}>‹</button><h2 style="margin:0;flex:1;text-align:center">Aylık rapor · ${MN[r.m-1]} ${r.y}</h2><button class="chip" id="rpn"${cur?" disabled":""}>›</button></div>`;
  if(!r.total)h+=`<p class="note">Bu ay kayıtlı oyun süresi yok. Süreler, sunucu açıkken oynadığın oyunlardan tutulur.</p>`;
  else{
    const diff=r.prev?Math.round((r.total-r.prev)/r.prev*100):null;
    h+=`<div class="sts">${tile(fmh(r.total),"Toplam"+(diff!=null?` · geçen aya göre ${diff>=0?"+":""}${diff}%`:""))+tile(r.days+" gün","Oynanan gün")+tile(r.sessions,"Oturum · ort. "+fmh(r.avg))+tile(r.streak+" gün","En uzun seri")}</div>`;
    if(r.longest)h+=`<p class="note" style="margin:12px 0 0">En uzun oturum: <b style="color:var(--text)">${esc(r.longest.name)}</b>, ${fmh(r.longest.min)} (${new Date(r.longest.t*1e3).toLocaleDateString("tr-TR",{day:"numeric",month:"long"})})</p>`;
    if(r.best_day)h+=`<p class="note" style="margin:6px 0 0">En yoğun gün: ${r.best_day.d} ${MN[r.m-1]}, ${fmh(r.best_day.min)}</p>`;
    const mx=Math.max(...r.top.map(g=>g.min),1);
    h+=`<p class="note" style="margin:16px 0 10px">En çok oynananlar</p>`+r.top.map((g,i)=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${i+1}. ${esc(g.name)}</span><i>${fmh(g.min)}</i></div><p><b style="width:${g.min/mx*100}%"></b></p></div></div>`).join("");
    const hm=Math.max(...r.hours,1);
    h+=`<p class="note" style="margin:16px 0 8px">Günün saatleri</p><div class="hrs">${r.hours.map((v,i)=>`<i title="${i}:00 · ${v} dk" style="height:${Math.max(v/hm*100,3)}%;${v?"":"opacity:.25"}"></i>`).join("")}</div><div class="hrl"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>`;
    const wm=Math.max(...r.wd,1),WD=["Pzt","Sal","Çar","Per","Cum","Cmt","Paz"];
    h+=`<p class="note" style="margin:16px 0 8px">Haftanın günleri</p><div class="chart" style="height:100px">${r.wd.map((v,i)=>`<div>${v?fmh(v).replace(" dk","dk").replace(" sa ","s"):""}<i style="height:${Math.max(v/wm*60,2)}px"></i><span>${WD[i]}</span></div>`).join("")}</div>`;
  }
  el.innerHTML=h;
  const pp=$("#rpp"),nn=$("#rpn");if(pp)pp.onclick=()=>loadReport(-1);if(nn)nn.onclick=()=>loadReport(1);
}
/* ---- Dosyalar ---- */
const fsz=b=>b>=2**30?(+(b/2**30).toFixed(2)).toLocaleString("tr-TR")+" GB":b>=2**20?(b/2**20).toFixed(1)+" MB":b>=1024?Math.round(b/1024)+" KB":b+" B";
let fMax=2**30,upBusy=false;
function filesView(){
  $("#main").innerHTML=`<header><h1>Dosyalar</h1></header>
  <div class="card"><h2>Telefondan PC'ye</h2><p class="note" id="fdir" style="margin-bottom:10px">Dosya başına en fazla 1 GB.</p>
  <input type="file" id="fu" multiple hidden><button class="btn" style="width:100%" id="fub">Dosya seç ve gönder</button><div id="fup" style="margin-top:12px"></div>
  <p class="note" style="margin-top:4px;font-size:12px">Büyük dosyalar gönderilirken ekranı kilitleme, uygulamadan çıkma.</p></div>
  <div class="card"><div class="row" style="margin:0 0 10px"><h2 style="margin:0">PC'den telefona</h2><span class="sp"></span><button class="chip" id="fop">Klasörü PC'de aç</button></div>
  <p class="note" style="margin-bottom:10px">PC'de bu klasöre attığın dosyalar burada görünür, "İndir"e basınca telefona iner.</p><div id="fl"><div class="empty" style="padding:14px 0">Yükleniyor…</div></div></div>
  <div class="card"><h2>Ekran görüntüleri</h2><div class="row" id="shf" style="flex-wrap:wrap;margin:0 0 10px"></div><div id="shg"><div class="empty" style="padding:14px 0">Yükleniyor…</div></div></div>`;
  loadShots();
  $("#fub").onclick=()=>{if(upBusy){toast("Gönderim sürüyor, bitmesini bekle.");return}$("#fu").click()};
  $("#fu").onchange=e=>{const fs=[...e.target.files];e.target.value="";if(fs.length)upload(fs)};
  $("#fop").onclick=async()=>{try{await post("/api/files",{action:"open"});toast("Klasör PC'de açılıyor.","ok")}catch(e){toast("PC'ye ulaşılamadı.","err")}};
  loadFiles();sysT=setInterval(()=>{if(!document.hidden)loadFiles()},8000);
}
async function loadFiles(){
  let j;try{const r=await fetch("/api/files",{headers:H()});if(!r.ok)throw 0;j=await r.json()}catch(e){if($("#fl"))$("#fl").innerHTML=`<p class="note">Dosya listesi alınamadı.</p>`;return}
  const el=$("#fl");if(!el)return;fMax=j.max||fMax;
  $("#fdir").textContent=`Dosya başına en fazla ${fsz(fMax)}. PC'de şuraya kaydedilir: ${j.dir}`;
  el.innerHTML=j.files.length?j.files.map(f=>`<div class="top"><div><div class="tn"><span>${esc(f.name)}</span><i>${fsz(f.size)}</i></div><span class="note">${new Date(f.t*1e3).toLocaleString("tr-TR",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}</span></div><button class="chip" data-fd="${esc(f.name)}">İndir</button><button class="chip" data-fx="${esc(f.name)}" style="color:var(--bad)">Sil</button></div>`).join(""):`<p class="note">Klasör boş.</p>`;
  el.querySelectorAll("[data-fd]").forEach(b=>b.onclick=()=>fileDown(b.dataset.fd));
  el.querySelectorAll("[data-fx]").forEach(b=>b.onclick=async()=>{if(!confirm(b.dataset.fx+" PC'den silinsin mi?"))return;try{await post("/api/files",{action:"del",name:b.dataset.fx});loadFiles()}catch(e){toast("Silinemedi.","err")}});
}
async function fileDown(name){
  try{const r=await post("/api/files",{action:"token",name});if(!r.ok)throw 0;const {t}=await r.json();
    const a=document.createElement("a");a.href="/dl?t="+encodeURIComponent(t);document.body.appendChild(a);a.click();a.remove();toast("İndirme başladı: "+name,"ok")}
  catch(e){toast("Dosya indirilemedi.","err")}
}
function upOne(f,row){
  return new Promise(res=>{
    const x=new XMLHttpRequest(),t0=Date.now();
    x.open("POST","/api/upload?name="+encodeURIComponent(f.name));x.setRequestHeader("X-Pin",pin);
    x.upload.onprogress=e=>{if(!e.lengthComputable||!row.isConnected)return;const s=(Date.now()-t0)/1e3,sp=s>0?e.loaded/s:0;
      row.querySelector("b").style.width=(e.loaded/e.total*100).toFixed(1)+"%";row.querySelector("i").textContent=`${Math.floor(e.loaded/e.total*100)}% · ${fsz(sp)}/sn`};
    x.onload=()=>res(x.status===200?"ok":x.status===413?"big":"err");x.onerror=x.onabort=()=>res("err");x.send(f);
  });
}
async function upload(fs){
  upBusy=true;let ok=0;
  for(const f of fs){
    const box=$("#fup");if(!box)break;
    const row=document.createElement("div");row.className="bar";row.innerHTML=`<div><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:8px">${esc(f.name)}</span><i>Bekliyor</i></div><p><b style="width:0"></b></p>`;box.prepend(row);
    if(f.size>fMax){row.querySelector("i").textContent="1 GB'tan büyük, atlandı";row.querySelector("i").style.color="var(--bad)";continue}
    const r=await upOne(f,row),i=row.querySelector("i");
    if(r==="ok"){ok++;i.textContent="Gönderildi · "+fsz(f.size);i.style.color="var(--ok)";row.querySelector("b").style.width="100%"}
    else{i.textContent=r==="big"?"Çok büyük":"Gönderilemedi";i.style.color="var(--bad)"}
  }
  upBusy=false;if(ok)toast(ok+" dosya PC'ye gönderildi.","ok");loadFiles();
}
/* ---- Profil ---- */
async function steamView(){
  $("#main").innerHTML=`<header><h1>Steam profilim</h1></header><div class="empty">Yükleniyor…</div>`;
  let p;
  try{const r=await fetch("/api/profile",{headers:H()});if(!r.ok)throw 0;p=await r.json()}
  catch(e){if(tab==="steam")$("#main").innerHTML=`<header><h1>Steam profilim</h1></header><div class="empty">Profil bilgisi alınamadı.</div>`;return}
  await poll();
  let act=null;try{act=await(await fetch("/api/activity",{headers:H()})).json()}catch(e){}
  if(tab!=="steam")return;
  const w=p.web,N=n=>Number(n).toLocaleString("tr-TR");
  const st=(v,l)=>`<div class="st"><b>${v}</b><span>${l}</span></div>`;
  const list=(a,u)=>{const m=Math.max(...a.map(g=>g.hours),1);return a.map(g=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${esc(g.name)}</span><i>${N(g.hours)} ${u||"sa"}</i></div><p><b style="width:${g.hours/m*100}%"></b></p></div></div>`).join("")};
  const playing=running.appid!=="0";
  const status=playing?"Oynuyor: "+(running.name||"bir oyun"):w&&w.state>0?"Çevrimiçi":"Steam";
  let h=`<header><div class="row" style="margin:0 0 10px"><button class="chip" id="pback">‹ Ayarlar</button><h1 style="margin:0">Steam profilim</h1></div></header><div class="card me">
    ${p.avatar?`<img class="av" alt="" src="data:image/png;base64,${p.avatar}">`:`<div class="av">${esc((p.name||"?")[0].toUpperCase())}</div>`}
    <div><h1>${esc(p.name)}</h1><small><span class="dot ${playing?"play":w&&w.state>0?"on":""}"></span>${esc(status)}</small></div>
    ${w&&w.level!=null?`<div class="lvl" title="Steam seviyesi">${w.level}</div>`:""}</div>`;
  let s=st(N(p.installed),"Kurulu oyun")+st(N(p.size_gb)+" GB","Disk kullanımı")+st(N(p.favs),"Favori");
  if(w){
    if(w.games!=null)s+=st(N(w.games),"Kütüphanedeki oyun")+st(N(w.hours)+" sa","Toplam oynama")+st(N(w.hours_2w)+" sa","Son 2 hafta");
    if(w.badges!=null)s+=st(N(w.badges),"Rozet");
    if(w.friends!=null)s+=st(N(w.friends),"Arkadaş");
    if(w.created)s+=st(new Date(w.created*1e3).getFullYear(),Math.floor((Date.now()/1e3-w.created)/31557600)+" yıllık üye");
    if(w.level!=null&&w.xp_next)s+=st(N(w.xp_next)+" XP","Sonraki seviyeye");
  }
  h+=`<div class="card" id="frc"><h2>Arkadaşlar</h2><div class="empty" style="padding:14px 0">Yükleniyor…</div></div>`;
  h+=`<div class="card"><h2>İstatistikler</h2><div class="sts">${s}</div></div>`;
  if(w&&w.top&&w.top.length)h+=`<div class="card"><h2>En çok oynananlar</h2>${list(w.top)}</div>`;
  if(w&&w.recent&&w.recent.length)h+=`<div class="card"><h2>Son 2 haftada</h2>${list(w.recent)}</div>`;
  if(act){
    const mx=Math.max(...act.days.map(d=>d.min),1),fm=m=>m>=60?(m/60).toFixed(1)+"sa":m+"dk";
    h+=`<div class="card"><h2>Bu hafta · ${act.total?fm(act.total):"0 dk"}${act.streak>1?" · "+act.streak+" gün seri":""}</h2><div class="chart">${act.days.map(d=>`<div>${d.min?fm(d.min):""}<i style="height:${Math.max(d.min/mx*80,2)}px"></i><span>${new Date(d.t*1e3).toLocaleDateString("tr-TR",{weekday:"short"})}</span></div>`).join("")}</div>
    ${act.top.length?`<div style="margin-top:14px">${list(act.top.map(g=>({appid:g.appid,name:g.name,hours:Math.round(g.min/6)/10})))}</div>`:`<p class="note" style="margin-top:10px">Henüz kayıt yok. Süreler bu program çalışırken oynadığın oyunlar için tutulur.</p>`}</div>`;
  }
  const big=games.filter(g=>g.size>0).sort((a,b)=>b.size-a.size).slice(0,5);
  if(big.length)h+=`<div class="card"><h2>En çok yer kaplayanlar</h2>${list(big.map(g=>({appid:g.appid,name:g.name,hours:Math.round(g.size/2**30*10)/10})),"GB")}</div>`;
  if(!p.has_key)h+=`<div class="card"><h2>Daha fazlası için</h2><p class="note">Oynama süreleri, seviye, rozet ve arkadaş sayısı için ücretsiz bir Steam API anahtarı gerekir. steamcommunity.com/dev/apikey adresinden al (alan adı kutusuna localhost yazabilirsin) ve Ayarlar'daki "Steam API anahtarı" kartına yapıştır. Steam profilinin ve "Oyun ayrıntıları"nın herkese açık olması gerekir.</p></div>`;
  else if(!w||w.games==null)h+=`<div class="card"><p class="note">Steam'den veri alınamadı. API anahtarını kontrol et ve Steam > Profil > Gizlilik ayarları'nda "Oyun ayrıntıları"nı herkese açık yap.</p></div>`;
  h+=sessCard(act);$("#main").innerHTML=h;$("#pback").onclick=()=>{tab="set";show()};loadFriends();
}
const PST=["Çevrimdışı","Çevrimiçi","Meşgul","Uzakta","Uykuda","Takas arıyor","Oyun arıyor"];
function seen(t){if(!t)return"Çevrimdışı";const d=(Date.now()/1e3-t)/86400;return "Son görülme: "+(d<1/24?"az önce":d<1?Math.floor(d*24)+" saat önce":d<2?"dün":d<30?Math.floor(d)+" gün önce":Math.floor(d/30)+" ay önce")}
async function loadFriends(showOff){
  const el=$("#frc");if(!el)return;
  let d;try{d=await(await fetch("/api/friends",{headers:H()})).json()}catch(e){d={error:"net"}}
  if(!$("#frc"))return;
  if(d.error){el.innerHTML=`<h2>Arkadaşlar</h2><p class="note">${d.error==="key"?"Arkadaşlarının kimin çevrimiçi olduğunu ve ne oynadığını görmek için Ayarlar'daki Steam API anahtarı kartına anahtarını ekle.":d.error==="private"?"Arkadaş listen gizli görünüyor. Steam > Profil > Profili düzenle > Gizlilik ayarları'nda \"Arkadaş listesi\"ni herkese açık yap.":"Arkadaş listesi şu an alınamadı."}</p>`;return}
  const f=d.friends,ing=f.filter(x=>x.game),on=f.filter(x=>!x.game&&x.st>0),off=f.filter(x=>!x.game&&!x.st);
  const row=x=>`<div class="fr">${x.av?`<img class="fa" alt="" src="${esc(x.av)}">`:`<div class="fa" style="display:flex;align-items:center;justify-content:center;font-weight:700">${esc((x.name||"?")[0].toUpperCase())}</div>`}<div><b>${esc(x.name)}</b><span class="${x.game?"g":x.st?"o":""}">${x.game?esc(x.game):x.st?PST[x.st]||"Çevrimiçi":seen(x.off)}</span></div>${x.gid&&games.some(g=>g.appid===x.gid)?`<button class="chip" data-fl="${x.gid}">Aç</button>`:""}${x.st?`<button class="chip" data-fc="${x.id}">Sohbet</button>`:""}</div>`;
  let h=`<h2>Arkadaşlar · ${ing.length+on.length} çevrimiçi</h2>`;
  if(ing.length)h+=`<div class="fh">Oyunda · ${ing.length}</div>`+ing.map(row).join("");
  if(on.length)h+=`<div class="fh">Çevrimiçi · ${on.length}</div>`+on.map(row).join("");
  if(!ing.length&&!on.length)h+=`<p class="note" style="margin-bottom:10px">Şu an çevrimiçi arkadaşın yok.</p>`;
  if(off.length)h+=showOff?`<div class="fh">Çevrimdışı · ${off.length}</div>`+off.map(row).join(""):`<button class="btn" style="width:100%;margin-top:6px" id="froff">Çevrimdışı olanları göster (${off.length})</button>`;
  el.innerHTML=h;
  el.querySelectorAll("[data-fc]").forEach(b=>b.onclick=async()=>{try{await post("/api/friendchat",{id:b.dataset.fc});toast("Sohbet PC'de açılıyor.","ok")}catch(e){toast("PC'ye ulaşılamadı.","err")}});
  el.querySelectorAll("[data-fl]").forEach(b=>b.onclick=()=>{const g=games.find(x=>x.appid===b.dataset.fl);if(g)launch(g.appid,g.name,null)});
  const fo=$("#froff");if(fo)fo.onclick=()=>loadFriends(true);
}
const COLORS={"Mavi":"#66c0f4","Yeşil":"#7bd88f","Mor":"#b794f6","Turuncu":"#ffb86b","Pembe":"#ff8fb8"};
const upt=m=>m>=60?Math.floor(m/60)+" sa "+(m%60)+" dk":m+" dk";
function pref(k,d){try{return localStorage.getItem(k)||d}catch(e){return d}}
const THEMES={"Steam":["#1b2838","#223247","#2f4560","#e6edf5","#8fa3b8"],"OLED siyah":["#000","#12161c","#232a33","#e6edf5","#8fa3b8"],"Gece":["#14122b","#1e1b3d","#322e5c","#ece9ff","#9a96c7"],"Orman":["#0f1f1a","#17302a","#264a40","#e3f2ec","#86a89b"],"Açık":["#f2f5f9","#ffffff","#d5dde7","#14202b","#5b6b7c"]};
const setLocal=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
function applyTheme(){
  let pt=null;try{pt=JSON.parse(pref("themeDef","null"))}catch(e){}
  const r=document.documentElement.style,t=THEMES[pref("theme","Steam")]||(pt&&pt.n===pref("theme","")?pt.t:null)||THEMES.Steam;
  if(t.length>5&&!THEMES[pref("theme","Steam")])r.setProperty("--accent",t[5]);
  ["--bg","--panel","--line","--text","--muted"].forEach((k,i)=>r.setProperty(k,t[i]));
  if(!(t.length>5&&!THEMES[pref("theme","Steam")]))r.setProperty("--accent",pref("accent","#66c0f4"));r.setProperty("--cw",pref("cw","150px"));r.setProperty("--r",pref("r","14px"));
  document.body.style.zoom=pref("zoom","1");
  document.body.classList.toggle("lst",pref("lay","grid")==="list");
  document.body.classList.toggle("nocov",pref("cov","1")==="0");
  const m=document.querySelector("meta[name=theme-color]");if(m)m.content=t[0];
}
function themeCard(){
  const c=pref("accent","#66c0f4"),w=pref("cw","150px");
  return `<div class="card"><h2>Görünüm</h2><div class="row" style="flex-wrap:wrap;margin:0 0 10px">${Object.entries(COLORS).map(([n,v])=>`<button class="chip${v===c?" on":""}" data-c="${v}">${n}</button>`).join("")}</div>
  <div class="row" style="margin:0">${[["Küçük kartlar","120px"],["Orta","150px"],["Büyük","200px"]].map(([n,v])=>`<button class="chip${v===w?" on":""}" data-cw="${v}">${n}</button>`).join("")}</div></div>`;
}
function bindTheme(){
  const on=(sel,key,attr)=>document.querySelectorAll(sel).forEach(b=>b.onclick=()=>{try{localStorage.setItem(key,b.dataset[attr])}catch(e){}applyTheme();document.querySelectorAll(sel).forEach(x=>x.classList.toggle("on",x===b))});
  on("[data-c]","accent","c");on("[data-cw]","cw","cw");
}
applyTheme();
document.addEventListener("error",e=>{
  const im=e.target;if(im.tagName!=="IMG")return;
  const m=im.src.match(/apps\/(\d+)\//);if(!m)return;
  const i=(+im.dataset.i||0)+1,L=IMGS(m[1]);
  if(i<L.length){im.dataset.i=i;im.src=L[i]}else{im.removeAttribute("src");im.classList.add("noimg")}
},true);
const probe=id=>new Promise(res=>{const L=IMGS(id);let i=0;const t=new Image();t.onload=()=>res(true);t.onerror=()=>{++i<L.length?t.src=L[i]:res(false)};t.src=L[0]});
async function scanImgs(){
  const b=$("#scan"),out=$("#scanout"),q=[...games],miss=[];let n=0;
  b.disabled=true;out.textContent="";
  await Promise.all(Array.from({length:8},async()=>{while(q.length){const g=q.shift();if(!await probe(g.appid))miss.push(g);b.textContent=`Taranıyor… ${++n}/${games.length}`}}));
  b.disabled=false;b.textContent="Yeniden tara";
  out.innerHTML=miss.length?`<p style="margin:10px 0 4px"><b>${miss.length} oyunun resmi yok:</b></p>`+miss.map(g=>`<div>• ${esc(g.name)} <i style="opacity:.6">(${g.appid})</i></div>`).join(""):`<p style="margin:10px 0 0">Hepsinin resmi var.</p>`;
}
function scanCard(){
  return `<div class="card"><h2>Kapak resmi olmayan oyunlar</h2><p class="note" style="margin-bottom:10px">Steam mağazasında bulunmayan, kaldırılmış ya da araç/demo türü uygulamaların kapağı olmaz. Tarayınca hangilerinin eksik olduğunu listeler.</p><button class="btn" style="width:100%" id="scan">Oyunları tara</button><div class="note" id="scanout"></div></div>`;
}
function lp(el,id){
  let t;const c=()=>clearTimeout(t);
  el.addEventListener("touchstart",()=>{t=setTimeout(()=>{el.dataset.lp=1;sheet(id)},550)},{passive:true});
  el.addEventListener("touchmove",c,{passive:true});el.addEventListener("touchend",c);el.addEventListener("touchcancel",c);
  el.addEventListener("click",e=>{if(el.dataset.lp){delete el.dataset.lp;e.stopImmediatePropagation();e.preventDefault()}},true);
}
function sheet(id){
  const g=games.find(x=>x.appid===id);if(!g)return;
  const d=document.createElement("div");d.id="sheet";
  d.innerHTML=`<div class="sh"><h2>${esc(g.name)}</h2><input type="text" id="nt" maxlength="60" placeholder="Not ekle (örn. co-op, arkadaşlarla)" value="${esc(g.note||"")}">
  <p class="note" style="margin:0 0 6px">Etiketler</p><div class="row" id="tgs" style="flex-wrap:wrap;margin:0 0 8px"></div>
  <div class="row" style="margin:0 0 12px"><input type="text" id="tgi" maxlength="20" placeholder="Yeni etiket" style="margin:0;flex:1;width:auto"><button class="btn" id="tga" style="padding:11px 16px">Ekle</button></div>
  <div class="btns"><button class="btn" id="nsv">Notu kaydet</button><button class="btn" id="nach">Başarımlar</button><button class="btn" data-o="store">Mağaza sayfası</button><button class="btn" data-o="props">Steam özellikleri</button><button class="btn" data-o="folder">Oyun klasörünü PC'de aç</button><button class="btn" data-sa="validate">Oyun dosyalarını doğrula</button><button class="btn bad" data-sa="uninstall">Oyunu kaldır</button><button class="btn bad" id="nhd">Listeden gizle</button><button class="btn" id="ncl">Kapat</button></div></div>`;
  document.body.appendChild(d);
  d.onclick=e=>{if(e.target===d)d.remove()};$("#ncl").onclick=()=>d.remove();d.querySelectorAll("[data-o]").forEach(b=>b.onclick=async()=>{d.remove();await post("/api/open",{appid:id,what:b.dataset.o});toast("PC'de açılıyor.","ok")});
  $("#nach").onclick=()=>{d.remove();openAch(id,g.name)};
  d.querySelectorAll("[data-sa]").forEach(b=>b.onclick=()=>{d.remove();steamApp(id,b.dataset.sa,g.name)});
  g.tags=g.tags||[];
  const saveT=async()=>{render();try{await post("/api/tags",{appid:id,tags:g.tags})}catch(e){toast("Etiket kaydedilemedi.","err")}};
  const drawT=()=>{const sug=[...new Set([...allTags(),...TAG_PRESETS])].filter(t=>!g.tags.includes(t));
    $("#tgs").innerHTML=g.tags.map(t=>`<button class="chip on" data-rt="${esc(t)}">#${esc(t)} ×</button>`).join("")+sug.map(t=>`<button class="chip" data-at="${esc(t)}">+ ${esc(t)}</button>`).join("");
    $("#tgs").querySelectorAll("[data-rt]").forEach(b=>b.onclick=()=>{g.tags=g.tags.filter(x=>x!==b.dataset.rt);drawT();saveT()});
    $("#tgs").querySelectorAll("[data-at]").forEach(b=>b.onclick=()=>{if(g.tags.length>=8){toast("En fazla 8 etiket.","err");return}g.tags.push(b.dataset.at);drawT();saveT()})};
  drawT();
  $("#tga").onclick=()=>{const v=$("#tgi").value.trim().replace(/^#/,"");if(!v)return;if(g.tags.length>=8){toast("En fazla 8 etiket.","err");return}if(!g.tags.includes(v))g.tags.push(v);$("#tgi").value="";drawT();saveT()};
  $("#tgi").onkeydown=e=>{if(e.key==="Enter")$("#tga").click()};
  $("#nsv").onclick=async()=>{g.note=$("#nt").value.trim();d.remove();render();try{await post("/api/note",{appid:id,text:g.note})}catch(e){toast("Not kaydedilemedi.","err")}};
  $("#nhd").onclick=async()=>{g.hidden=true;d.remove();render();toast("Gizlendi. Geri getirmek için Profil sekmesine bak.","ok");try{await post("/api/hide",{appid:id,hidden:true})}catch(e){}};
}
const fmd=m=>m>=60?(m/60).toFixed(1)+" sa":m+" dk";
const APP_PRESETS=[["Discord","discord://"],["Spotify","spotify:"],["YouTube","https://www.youtube.com"],["Twitch","https://www.twitch.tv"],["Not Defteri","notepad.exe"]];
function schedCard(cfg){
  const opts=games.filter(g=>!g.hidden).map(g=>`<option value="${g.appid}">${esc(g.name)}</option>`).join("");
  return `<div class="card"><h2>Oyun planlayıcı</h2><p class="note" style="margin-bottom:10px">Belirlediğin saatte oyun PC'de kendiliğinden açılır (PC açık ve program çalışıyor olmalı).</p>
  ${(cfg.schedules||[]).map((s,i)=>`<div class="top"><div><div class="tn"><span>${esc(s.name)}</span><i>${s.time}</i></div></div><button class="chip" data-sd="${i}">Sil</button></div>`).join("")}
  <select id="sg">${opts}</select><input type="time" id="st" value="20:00"><button class="btn" style="width:100%" id="sadd">Planla</button></div>`;
}
function appsCard(cfg){
  return `<div class="card"><h2>Programlar</h2><p class="note" style="margin-bottom:10px">PC'de açmak istediğin program, site ya da klasör. Bilgisayar sekmesinde düğme olarak görünür.</p>
  ${(cfg.apps||[]).map((a,i)=>`<div class="top"><div><div class="tn"><span>${esc(a.name)}</span></div><span class="note">${esc(a.path)}</span></div><button class="chip" data-ad="${i}">Sil</button></div>`).join("")}
  <div class="row" style="flex-wrap:wrap;margin:8px 0">${APP_PRESETS.map(([n,p])=>`<button class="chip" data-ap="${esc(p)}" data-an="${n}">+ ${n}</button>`).join("")}</div>
  <input type="text" id="an" placeholder="Ad (örn. OBS)"><input type="text" id="ap" placeholder="Yol veya adres (örn. C:\\Program Files\\obs\\obs64.exe)"><button class="btn" style="width:100%" id="aadd">Ekle</button></div>`;
}
function pinCard(){
  return `<div class="card"><h2>Dil / Language</h2><div class="row" style="flex-wrap:wrap;margin:0" id="langrow"><button class="chip" data-lang="tr">Türkçe</button><button class="chip" data-lang="en">English</button></div></div><div class="card"><h2>Sunucu</h2><p class="note" style="margin-bottom:10px">Sunucu arka planda penceresiz çalışır. Durdurmak istersen buradan kapat. Tekrar açmak için PC'de steam_remote.py'ye çift tıkla.</p><button class="btn" style="width:100%" id="srvoff">Sunucuyu kapat</button></div><div class="card"><h2>PIN</h2><p class="note" style="margin-bottom:10px">PIN'i buradan değiştirebilirsin (4–12 rakam). Kestirmeler bağlantılarında eski PIN yazılıysa onları yenilemen gerekir.</p>
  <input type="password" inputmode="numeric" id="np" placeholder="Yeni PIN" style="width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:15px;margin-bottom:8px;outline:none"><button class="btn" style="width:100%" id="npb">PIN'i değiştir</button></div>`;
}
async function loadApps(){
  try{const c=await(await fetch("/api/config",{headers:H()})).json(),b=$("#appb");if(!b)return;
    $("#appsc").hidden=!c.apps.length;b.innerHTML=c.apps.map((a,i)=>`<button class="btn" data-ar="${i}">${esc(a.name)}</button>`).join("");
    b.querySelectorAll("[data-ar]").forEach(x=>x.onclick=async()=>{await post("/api/apps",{action:"run",i:+x.dataset.ar});toast(x.textContent+" açılıyor.","ok")})}catch(e){}
}
function clipBind(){
  $("#cbs").onclick=async()=>{const t=$("#cb").value;if(!t)return;try{await post("/api/clipboard",{text:t});toast("PC panosuna kopyalandı.","ok")}catch(e){toast("PC'ye ulaşılamadı.","err")}};
  $("#cbg").onclick=async()=>{try{const j=await(await post("/api/clipboard",{})).json();$("#cbo").textContent=j.text||"(PC panosu boş)";$("#cb").value=j.text||""}catch(e){toast("PC'ye ulaşılamadı.","err")}};
}
async function settingsView(){
  $("#main").innerHTML=`<header><h1>Ayarlar</h1></header><div class="empty">Yükleniyor…</div>`;
  await poll();
  let act=null,cfg={apps:[],schedules:[]};
  try{act=await(await fetch("/api/activity",{headers:H()})).json()}catch(e){}
  try{cfg=await(await fetch("/api/config",{headers:H()})).json()}catch(e){}
  if(tab!=="set")return;
  const ch=(k,d,arr)=>`<div class="row" style="flex-wrap:wrap;margin:0 0 12px">${arr.map(([n,v])=>`<button class="chip${pref(k,d)===v?" on":""}" data-k="${k}" data-v="${v}">${n}</button>`).join("")}</div>`;
  const lb=t=>`<p class="note" style="margin:0 0 6px">${t}</p>`;
  const ac=pref("accent","#66c0f4");
  $("#main").innerHTML=`<header><h1>Ayarlar</h1></header>
  <div class="btns" style="margin-bottom:12px"><button class="btn" id="ssteam">Steam profilim</button>${ME.standalone?"":'<button class="btn" id="sadmin">Admin</button>'}</div>
  <div class="card"><h2>Özelleştirme</h2>
  ${lb("Tema")}${ch("theme","Steam",Object.keys(THEMES).map(n=>[n,n]))}
  ${lb("Vurgu rengi")}<div class="row" style="flex-wrap:wrap;margin:0 0 12px">${Object.entries(COLORS).map(([n,v])=>`<button class="chip${ac===v?" on":""}" data-k="accent" data-v="${v}">${n}</button>`).join("")}<input type="color" id="cc" value="${ac}" style="width:44px;height:34px;border:0;background:none;padding:0"></div>
  ${lb("Oyun listesi düzeni")}${ch("lay","grid",[["Izgara","grid"],["Liste","list"]])}
  ${lb("Kart boyutu")}${ch("cw","150px",[["Küçük","120px"],["Orta","150px"],["Büyük","200px"]])}
  ${lb("Köşe yuvarlaklığı")}${ch("r","14px",[["Keskin","4px"],["Orta","14px"],["Yuvarlak","24px"]])}
  ${lb("Yazı boyutu")}${ch("zoom","1",[["Küçük","0.92"],["Normal","1"],["Büyük","1.1"]])}
  ${lb("Kapak resimleri")}${ch("cov","1",[["Göster","1"],["Gizle (kompakt)","0"]])}
  ${lb("Devam et kartı")}${ch("hero","1",[["Göster","1"],["Gizle","0"]])}
  ${lb("Son oynananlar ve favori şeritleri")}${ch("strips","1",[["Göster","1"],["Gizle","0"]])}
  ${lb("Ses efektleri (jeton, çark, mesaj)")}${ch("sfx","1",[["Açık","1"],["Kapalı","0"]])}
  ${lb("Uygulama açılınca ilk sekme")}${ch("tab","home",[["Özet","home"],["Oyunlar","games"],["Sohbet","chat"],["Bilgisayar","pc"],["Dosyalar","files"],["Profil","profile"]])}
  <button class="btn" style="width:100%" id="rst">Görünümü sıfırla</button></div>
  <div class="card" id="addrc"><h2>Adres & QR kod</h2><p class="note">Yükleniyor…</p></div>
  ${prefCard(act)}<div class="card" id="gmc"><h2>Oyun modu</h2><p class="note">Yükleniyor…</p></div>${notifyCard(cfg)}${discordCard(cfg)}${keyCard(cfg)}${schedCard(cfg)}${appsCard(cfg)}${androidCard()}${pinCard()}${hiddenCard()}${scanCard()}`;
  document.querySelectorAll("[data-k]").forEach(b=>b.onclick=()=>{setLocal(b.dataset.k,b.dataset.v);applyTheme();document.querySelectorAll(`[data-k="${b.dataset.k}"]`).forEach(x=>x.classList.toggle("on",x===b))});
  $("#cc").oninput=e=>{setLocal("accent",e.target.value);applyTheme();document.querySelectorAll('[data-k="accent"]').forEach(x=>x.classList.remove("on"))};
  $("#rst").onclick=()=>{["theme","accent","lay","cw","r","zoom","cov","hero","strips","tab"].forEach(k=>{try{localStorage.removeItem(k)}catch(e){}});applyTheme();settingsView()};
  bindProf();$("#scan").onclick=scanImgs;
  $("#sadd").onclick=async()=>{await post("/api/schedule",{action:"add",appid:$("#sg").value,time:$("#st").value});toast("Planlandı.","ok");settingsView()};
  document.querySelectorAll("[data-sd]").forEach(b=>b.onclick=async()=>{await post("/api/schedule",{action:"del",i:+b.dataset.sd});settingsView()});
  $("#aadd").onclick=async()=>{const n=$("#an").value.trim(),p=$("#ap").value.trim();if(!n||!p){toast("Ad ve yol gerekli.","err");return}await post("/api/apps",{action:"add",name:n,path:p});settingsView()};
  document.querySelectorAll("[data-ap]").forEach(b=>b.onclick=async()=>{await post("/api/apps",{action:"add",name:b.dataset.an,path:b.dataset.ap});settingsView()});
  document.querySelectorAll("[data-ad]").forEach(b=>b.onclick=async()=>{await post("/api/apps",{action:"del",i:+b.dataset.ad});settingsView()});
  bindNotify(cfg);loadGm();loadAddr();
  $("#ssteam").onclick=()=>{tab="steam";show()};(()=>{const _a=$("#sadmin");if(_a)_a.onclick=openAdmin})();
  $("#srvoff").onclick=async()=>{if(!confirm("Sunucu kapatılsın mı? Tekrar açmak için PC'de dosyaya çift tıklaman gerekir."))return;
    await post("/api/server",{action:"stop"});toast("Sunucu kapatıldı.","ok")};
  $("#npb").onclick=async()=>{const v=$("#np").value.trim();if(!/^\d{4,12}$/.test(v)){toast("PIN 4–12 rakam olmalı.","err");return}
    if(!confirm("PIN değiştirilsin mi?"))return;
    const r=await post("/api/pin",{pin:v});if(r.ok){pin=v;setLocal("pin",v);$("#np").value="";toast("PIN değişti.","ok")}else toast("Değiştirilemedi.","err")};
  document.querySelectorAll("[data-lang]").forEach(b=>{if(b.dataset.lang===LANG)b.classList.add("on");b.onclick=async()=>{if(b.dataset.lang===LANG)return;try{localStorage.setItem("lang",b.dataset.lang)}catch(e){}try{await post("/api/lang",{lang:b.dataset.lang})}catch(e){}location.reload()}});
}
const NT=[["chat","Sohbet mesajları"],["sale","İstek listesinde indirim"],["dl","İndirme bitti"],["crash","Oyun çöktü"],["sched","Planlı oyun açıldı"],["timer","Zamanlayıcı oyunu kapattı"],["brk","Mola hatırlatıcısı"],["goal","Günlük hedef"],["game","Oyun açıldı / kapandı"]];
const DT=[["dc_start","Oyun açılınca duyur"],["dc_end","Oyun bitince duyur"],["dc_copy","Bildirimleri buraya da gönder"]];
const ntChips=(arr,n)=>`<div class="row" style="flex-wrap:wrap;margin:0">${arr.map(([k,l])=>`<button class="chip${n[k]?" on":""}" data-nt="${k}">${l}</button>`).join("")}</div>`;
function notifyCard(c){
  return `<div class="card"><h2>Telefona bildirimler</h2><p class="note" style="margin-bottom:10px">Telefonuna ücretsiz <b>ntfy</b> uygulamasını kur (App Store veya Google Play). Uygulamada + işaretine bas, aşağıdaki konu adını yaz ve abone ol. Konu adı şifre gibidir, kimseyle paylaşma.</p>
  <input type="text" id="ntt" value="${esc(c.ntfy_topic||c.ntfy_suggest||"")}" placeholder="Konu adı" autocomplete="off" autocapitalize="off"><div class="btns" style="margin-bottom:12px"><button class="btn" id="nts">${c.ntfy_topic?"Kaydet":"Kaydet ve aç"}</button><button class="btn" id="ntx">Test bildirimi</button>${c.ntfy_topic?`<button class="btn bad" id="ntd">Kapat</button>`:""}</div>
  <p class="note" style="margin-bottom:6px">${c.ntfy_topic?"Bildirimler açık. Hangi durumlarda gelsin?":"Kaydedince bu durumlarda bildirim gelir:"}</p>${ntChips(NT,c.nt||{})}</div>`;
}
function discordCard(c){
  return `<div class="card"><h2>Discord</h2><p class="note" style="margin-bottom:10px">Discord'da bir kanalın ayarlarını aç: Entegrasyonlar > Webhook'lar > Yeni Webhook. "Webhook URL'sini Kopyala"ya bas ve buraya yapıştır. Oyun duyuruları ve arkadaş çağırma mesajları o kanala gider.</p>
  <input type="text" id="dwh" autocomplete="off" autocapitalize="off" placeholder="${c.has_webhook?"Webhook kayıtlı. Değiştirmek için yenisini yapıştır":"https://discord.com/api/webhooks/…"}"><div class="btns" style="margin-bottom:12px"><button class="btn" id="dws">Kaydet</button><button class="btn" id="dwx">Test mesajı</button>${c.has_webhook?`<button class="btn bad" id="dwd">Kaldır</button>`:""}</div>
  ${ntChips(DT,c.nt||{})}</div>`;
}
function keyCard(c){
  const warn=`<p class="note" style="margin-bottom:10px;color:var(--warn)">⚠️ Girdiğin anahtar merkez sunucuya (yöneticinin bilgisayarına) kaydedilir ve yönetici anahtarı görebilir. Anahtarını kimseyle paylaşmak istemiyorsan boş bırak.</p>`;
  return `<div class="card"><h2>Steam API anahtarı</h2><p class="note" style="margin-bottom:10px">${c.has_key?"Anahtar kayıtlı. Profil istatistikleri ve arkadaş durumu açık.":"Profil istatistikleri ve arkadaşlarının durumu için gerekir. steamcommunity.com/dev/apikey adresinden ücretsiz al (alan adına localhost yaz) ve buraya yapıştır."}</p>${warn}
  <input type="password" id="ak" autocomplete="off" placeholder="${c.has_key?"Yeni anahtar (değiştirmek için)":"32 karakterlik API anahtarı"}"><div class="btns"><button class="btn" id="aks">Kaydet</button>${c.has_key?`<button class="btn bad" id="akd">Kaldır</button>`:""}</div></div>`;
}
function androidCard(){
  return window.Android?`<div class="card"><h2>Android uygulaması</h2><p class="note" style="margin-bottom:10px">PC'nin ev ve Tailscale adreslerini buradan değiştirebilirsin.</p><button class="btn" style="width:100%" id="andset">Sunucu adresleri</button></div>`:"";
}
function bindNotify(c){
  const n=c.nt||{},sv=async(o,okMsg,errMsg,re)=>{try{const r=await post("/api/notify",o);toast(r.ok?okMsg:errMsg,r.ok?"ok":"err");if(r.ok&&re)settingsView();return r.ok}catch(e){toast("PC'ye ulaşılamadı.","err")}};
  document.querySelectorAll("[data-nt]").forEach(b=>b.onclick=async()=>{const k=b.dataset.nt;n[k]=!n[k];b.classList.toggle("on",n[k]);try{await post("/api/notify",{nt:{[k]:n[k]}})}catch(e){toast("Kaydedilemedi.","err")}});
  $("#nts").onclick=()=>sv({ntfy_topic:$("#ntt").value.trim()},"Kaydedildi. Test bildirimi gönderebilirsin.","Konu adında sadece harf, rakam, - ve _ olabilir.",true);
  if($("#ntd"))$("#ntd").onclick=()=>sv({ntfy_topic:""},"Bildirimler kapatıldı.","",true);
  $("#ntx").onclick=()=>testN("ntfy");$("#dwx").onclick=()=>testN("discord");
  $("#dws").onclick=()=>{const v=$("#dwh").value.trim();if(v)sv({webhook:v},"Webhook kaydedildi.","Bu bir Discord webhook adresi değil.",true)};
  if($("#dwd"))$("#dwd").onclick=()=>sv({webhook:""},"Webhook kaldırıldı.","",true);
  $("#aks").onclick=()=>{const v=$("#ak").value.trim();if(!v)return;if(!confirm("Bu anahtar merkez veritabanına kaydedilecek ve yönetici görebilecek. Kaydedilsin mi?"))return;sv({apikey:v},"Anahtar kaydedildi.","Anahtar 32 karakter olmalı (rakam ve A–F).",true)};
  if($("#akd"))$("#akd").onclick=()=>sv({apikey:""},"Anahtar kaldırıldı.","",true);
  if($("#andset"))$("#andset").onclick=()=>window.Android.openSettings();
}
async function testN(to){
  try{const j=await(await post("/api/notify-test",{to})).json();
    toast(j.ok?(to==="ntfy"?"Test gönderildi, telefonuna bak.":"Test mesajı kanala gönderildi."):j.error==="none"?(to==="ntfy"?"Önce konu adını kaydet.":"Önce webhook'u kaydet."):"Gönderilemedi: "+j.error,j.ok?"ok":"err")}
  catch(e){toast("PC'ye ulaşılamadı.","err")}
}
function sessCard(act){
  if(!act||!act.recent||!act.recent.length)return"";
  return `<div class="card"><h2>Son oturumlar</h2>${act.recent.map(r=>`<div class="top"><img alt="" src="${img(r.appid)}"><div><div class="tn"><span>${esc(r.name)}</span><i>${fmd(r.min)}</i></div><span class="note">${new Date(r.t*1e3).toLocaleString("tr-TR",{weekday:"short",hour:"2-digit",minute:"2-digit"})}</span></div></div>`).join("")}</div>`;
}
function prefCard(act){
  const br=running.break||0,gl=running.goal||0,today=act&&act.days?act.days[6].min:0;
  const chips=(a,cur,arr)=>arr.map(([n,v])=>`<button class="chip${v===cur?" on":""}" data-${a}="${v}">${n}</button>`).join("");
  return `<div class="card"><h2>Mola ve günlük hedef</h2>
  <p class="note" style="margin-bottom:8px">Mola hatırlatıcısı: oyun sırasında PC ekranında "mola ver" mesajı çıkar.</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 12px">${chips("br",br,[["Kapalı",0],["45 dk",45],["60 dk",60],["90 dk",90]])}</div>
  <p class="note" style="margin-bottom:8px">Günlük hedef: aşılınca PC ekranında uyarı çıkar.</p>
  <div class="row" style="flex-wrap:wrap;margin:0 0 ${gl?12:0}px">${chips("gl",gl,[["Kapalı",0],["1 sa",60],["2 sa",120],["3 sa",180],["4 sa",240]])}</div>
  ${gl?`<div class="bar" style="margin:0"><div><span>Bugün</span><i>${today} / ${gl} dk</i></div><p><b style="width:${Math.min(100,today/gl*100)}%"></b></p></div>`:""}</div>`;
}
function hiddenCard(){
  const h=games.filter(g=>g.hidden);
  return h.length?`<div class="card"><h2>Gizli oyunlar</h2>${h.map(g=>`<div class="top"><img alt="" src="${img(g.appid)}"><div><div class="tn"><span>${esc(g.name)}</span><button class="chip" data-un="${g.appid}">Geri getir</button></div></div></div>`).join("")}</div>`:"";
}
async function setPref(o,sel,b){
  Object.assign(running,o);document.querySelectorAll(sel).forEach(x=>x.classList.toggle("on",x===b));
  try{await post("/api/settings",o);toast("Kaydedildi.","ok")}catch(e){toast("Kaydedilemedi.","err")}
}
function bindProf(){
  document.querySelectorAll("[data-br]").forEach(b=>b.onclick=()=>setPref({break:+b.dataset.br},"[data-br]",b));
  document.querySelectorAll("[data-gl]").forEach(b=>b.onclick=()=>setPref({goal:+b.dataset.gl},"[data-gl]",b));
  document.querySelectorAll("[data-un]").forEach(b=>b.onclick=async()=>{const g=games.find(x=>x.appid===b.dataset.un);g.hidden=false;b.closest(".top").remove();try{await post("/api/hide",{appid:g.appid,hidden:false})}catch(e){}});
}
let sort="recent",liveOn=false,qw=720,ctl=false,rc=false;
pin?load():loginView();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("image") else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        given = self.headers.get("X-Pin", "").encode()
        ok = hmac.compare_digest(given, get_pin().encode())
        self.role = "full"
        gp = guest_pin()
        if not ok and gp and hmac.compare_digest(given, gp.encode()):
            ok, self.role = True, "guest"
            allowed = GUEST_GET if self.command == "GET" else GUEST_POST
            if self.path.split("?")[0] not in allowed or (self.path == "/api/stop" and not _data().get("guest_stop")):
                self._send(403, '{"error":"guest"}')
                return False
        if not ok:
            time.sleep(1)  # kaba kuvvete karşı yavaşlat
            self.close_connection = True
            self._send(401, '{"error":"pin"}')
        return ok

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    def _upload(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = safe_name((q.get("name") or ["dosya"])[0])
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self._send(411, '{"error":"length"}')
        if n > MAX_UP:
            self.close_connection = True
            return self._send(413, '{"error":"big"}')
        path = unique_path(name)
        part = path + "." + secrets.token_hex(3) + ".part"
        left = n
        try:
            with open(part, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise IOError("bağlantı koptu")
                    f.write(chunk)
                    left -= len(chunk)
            if os.path.exists(path):
                path = unique_path(os.path.basename(path))
            os.replace(part, path)
        except Exception as e:
            log(f"dosya alınamadı ({name}): {e}")
            try:
                os.remove(part)
            except OSError:
                pass
            self.close_connection = True
            return self._send(500, '{"error":"save"}')
        log(f"telefondan dosya geldi: {os.path.basename(path)} ({n} bayt)")
        return self._send(200, json.dumps({"ok": True, "name": os.path.basename(path)}))

    def _download(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = _dl_tokens.get((q.get("t") or [""])[0])
        path = share_file(tok[0]) if tok and tok[1] > time.time() else None
        if not path:
            return self._send(404, "Dosya bulunamadı ya da bağlantının süresi doldu.", "text/plain")
        name = os.path.basename(path)
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        ascii_name = re.sub(r'[^A-Za-z0-9._ ()-]', "_", name)
        self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'' + urllib.parse.quote(name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        if self.path.startswith("/dl?"):
            return self._download()
        if self.path.startswith("/cimg?"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            f = q.get("f", "")
            if not hmac.compare_digest(q.get("t", "").encode(), _chat_rt["tok"].encode()) or not re.fullmatch(r"[a-f0-9]{1,16}\.(jpg|wav)", f):
                return self._send(404, "yok", "text/plain")
            try:
                with open(os.path.join(CHAT_MEDIA, f), "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._send(404, "yok", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav" if f.endswith(".wav") else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/shot?"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            lst = _shots["list"]
            if not hmac.compare_digest(q.get("t", "").encode(), _shots["tok"].encode()) or not q.get("id", "").isdigit() \
                    or int(q["id"]) >= len(lst):
                return self._send(404, "yok", "text/plain")
            it = lst[int(q["id"])]
            try:
                if q.get("th"):
                    data, ct = shot_thumb(it)
                    return self._send(200, data, ct)
                with open(it["path"], "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, "yok", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(it["path"])[0] or "image/png")
            self.send_header("Content-Length", str(len(data)))
            if q.get("dl"):
                nm = os.path.basename(it["path"])
                self.send_header("Content-Disposition", f'attachment; filename="{re.sub(r"[^A-Za-z0-9._ ()-]", "_", nm)}"')
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith(("/icon", "/apple-touch-icon")):
            m = re.search(r"(\d{2,3})", self.path)
            return self._send(200, icon_png(max(48, min(512, int(m.group(1)))) if m else 180), "image/png")
        if self.path.startswith("/asset?"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            f = q.get("f", "")
            if not ASSET_RE.fullmatch(f):
                return self._send(404, "yok", "text/plain")
            try:
                with open(os.path.join(ASSETS, f), "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._send(404, "yok", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", ASSET_CT[f.rsplit(".", 1)[1]])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/qr":
            return self._send(200, QR_PAGE, "text/html")
        if self.path == "/qr.json":
            a = addresses()
            return self._send(200, json.dumps({"lan": a["lan"], "ts": a["ts"], "pin_default": get_pin() == "1234"}))
        if self.path == "/manifest.webmanifest":
            return self._send(200, manifest(), "application/manifest+json")
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html")
        if self.path.startswith("/q?"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            if not hmac.compare_digest(q.get("pin", "").encode(), get_pin().encode()):
                time.sleep(1)
                return self._send(401, "PIN yanlış", "text/plain")
            try:
                return self._send(200, quick_command(q), "text/plain")
            except Exception as e:
                log(f"kestirme hatası: {e}")
                return self._send(500, "Hata", "text/plain")
        if not self.path.startswith("/api/"):
            return self._send(404, '{"error":"yok"}')
        if not self._authed():
            return
        if self.path == "/api/games":
            favs = load_favs()
            pd = _data()
            notes, hidden, tags = pd.get("notes", {}), set(pd.get("hidden", [])), pd.get("tags", {})
            out = [{"appid": g["appid"], "name": g["name"], "last": g["last"], "size": g["size"], "fav": g["appid"] in favs,
                    "note": notes.get(g["appid"], ""), "hidden": g["appid"] in hidden, "tags": tags.get(g["appid"], [])}
                   for g in read_games(force=True)]
            self._send(200, json.dumps(out))
        elif self.path == "/api/status":
            cur = running_appid()
            g = find_game(cur) if cur != "0" else None
            el = int(time.time() - _state["since"]) if cur != "0" and _state["since"] and _state["app"] == cur else 0
            self._send(200, json.dumps({"appid": cur, "name": g["name"] if g else "", "elapsed": el,
                                         "kill_in": max(0, int(_state["kill_at"] - time.time())) if _state["kill_at"] else 0,
                                         **{k: _data().get(k, 0) for k in ("break", "goal")}}))
        elif self.path == "/api/profile":
            self._send(200, json.dumps(steam_profile()))
        elif self.path == "/api/config":
            d = _data()
            sc = [{**x, "name": (find_game(x["appid"]) or {}).get("name", x["appid"])} for x in d.get("schedules", [])]
            self._send(200, json.dumps({"apps": d.get("apps", []), "schedules": sc, "has_key": bool(api_key()),
                                        "ntfy_topic": d.get("ntfy_topic", ""),
                                        "ntfy_suggest": "steam-" + secrets.token_hex(5),
                                        "has_webhook": bool(d.get("webhook")), "nt": nt_settings(d)}))
        elif self.path.startswith("/api/ach?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            appid = (q.get("appid") or [""])[0]
            self._send(200, json.dumps(achievements(appid) if appid.isdigit() else {"error": "bad"}))
        elif self.path == "/api/library":
            self._send(200, json.dumps(library_info()))
        elif self.path.startswith("/api/report"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lt = time.localtime()
            try:
                y, m = (int(x) for x in (q.get("m") or [f"{lt.tm_year}-{lt.tm_mon}"])[0].split("-"))
                assert 2000 <= y <= 2100 and 1 <= m <= 12
            except Exception:
                y, m = lt.tm_year, lt.tm_mon
            self._send(200, json.dumps(month_report(y, m)))
        elif self.path == "/api/home":
            self._send(200, json.dumps(home_info()))
        elif self.path == "/api/chat":
            self._send(200, json.dumps(chat_state()))
        elif self.path == "/api/me":
            self._send(200, json.dumps(me_info()))
        elif self.path == "/api/whoami":
            self._send(200, json.dumps({"role": self.role, "name": persona(), "guest_stop": bool(_data().get("guest_stop"))}))
        elif self.path == "/api/addr":
            self._send(200, json.dumps({**addresses(), "guest": bool(guest_pin()), "guest_stop": bool(_data().get("guest_stop"))}))
        elif self.path == "/api/board":
            self._send(200, json.dumps(board_get()))
        elif self.path == "/api/quiz":
            self._send(200, json.dumps(quiz_today()))
        elif self.path == "/api/discover":
            self._send(200, json.dumps({"rec": recommendations(), "coplay": coplay_list()}))
        elif self.path.startswith("/api/chat/msgs?"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            self._send(200, json.dumps(chat_msgs(q.get("id", ""), _int(q.get("after", "0")))))
        elif self.path == "/api/audio-devices":
            self._send(200, json.dumps(audio_devices()))
        elif self.path == "/api/app-volumes":
            self._send(200, json.dumps(app_volumes()))
        elif self.path == "/api/macros":
            self._send(200, json.dumps({"macros": _data().get("macros", [])}))
        elif self.path == "/api/backups":
            self._send(200, json.dumps({"backups": backup_list(), "dir": BACKUP_DIR}))
        elif self.path == "/api/procs":
            try:
                self._send(200, json.dumps(proc_list()))
            except Exception as e:
                log(f"görev listesi: {e}")
                self._send(500, '{"error":"procs"}')
        elif self.path == "/api/monitors":
            self._send(200, json.dumps(monitors()))
        elif self.path.startswith("/api/wishlist"):
            self._send(200, json.dumps(wishlist(force="force" in self.path)))
        elif self.path == "/api/shots":
            lst = screenshots()
            self._send(200, json.dumps({"tok": _shots["tok"], "list": [
                {"id": i, "appid": x["appid"], "name": (find_game(x["appid"]) or {}).get("name", "") if x["appid"] else "",
                 "src": x["src"], "t": x["t"], "size": x["size"]} for i, x in enumerate(lst)]}))
        elif self.path == "/api/config-gm":
            self._send(200, json.dumps({"gm": gm_settings(), "apps": [a["name"] for a in _data().get("apps", [])]}))
        elif self.path == "/api/mic":
            if not audio_lib():
                self._send(501, json.dumps({"error": "lib", "busy": _audio_inst["busy"], "msg": _audio_inst["msg"]}))
            else:
                audio_stream(self, mic=True)
        elif self.path == "/api/audio":
            if not audio_lib():
                self._send(501, json.dumps({"error": "lib", "busy": _audio_inst["busy"], "msg": _audio_inst["msg"]}))
            else:
                audio_stream(self)
        elif self.path == "/api/files":
            self._send(200, json.dumps({"files": list_share(), "dir": share_dir(), "max": MAX_UP}))
        elif self.path == "/api/friends":
            self._send(200, json.dumps(friends_status()))
        elif self.path == "/api/perf":
            _perf["req"] = time.time()
            h = _perf["hist"]
            self._send(200, json.dumps({"now": h[-1] if h else None, "hist": h}))
        elif self.path == "/api/activity":
            self._send(200, json.dumps(activity()))
        elif self.path == "/api/system":
            self._send(200, json.dumps(system_info()))
        elif self.path.startswith("/api/screenshot"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                w = max(320, min(1600, int((q.get("w") or ["1100"])[0])))
            except ValueError:
                w = 1100
            shot = screenshot(w)
            self._send(200, shot[0], shot[1]) if shot else self._send(500, '{"error":"shot"}')
        else:
            self._send(404, '{"error":"yok"}')

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, '{"error":"yok"}')
        if not self._authed():
            return
        if self.path.startswith("/api/upload"):
            return self._upload()
        if self.path == "/api/me/bg":
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if not 100 < n <= 2 * 1024 * 1024:
                self.close_connection = True
                return self._send(413, '{"error":"big"}')
            return self._send(200, json.dumps(custom_bg_upload(self.rfile.read(n))))
        if self.path.startswith("/api/chat/voice"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if not 1000 < n <= 1600 * 1024:
                self.close_connection = True
                return self._send(413, '{"error":"big"}')
            data = self.rfile.read(n)
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                return self._send(400, '{"error":"wav"}')
            err = chat_send(q.get("id", ""), "voice", data=data, dur=_int(q.get("dur", "0")))
            return self._send(200, json.dumps({"error": err} if err else {"ok": True}))
        if self.path.startswith("/api/chat/img"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if not 100 < n <= 1950 * 1024:
                self.close_connection = True
                return self._send(413, '{"error":"big"}')
            data = self.rfile.read(n)
            if data[:3] != b"\xff\xd8\xff":
                return self._send(400, '{"error":"jpeg"}')
            err = chat_send(q.get("id", ""), "img", data=data)
            return self._send(200, json.dumps({"error": err} if err else {"ok": True}))
        if self.path == "/api/speak":
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if not 44 < n <= 8 * 2**20:
                self.close_connection = True
                return self._send(413, '{"error":"size"}')
            data = self.rfile.read(n)
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                return self._send(400, '{"error":"wav"}')
            play_wav(data)
            log(f"telefondan sesli mesaj çalındı ({n} bayt)")
            return self._send(200, '{"ok":true}')
        body = self._body()
        if body is None:
            return self._send(400, '{"error":"bad"}')
        ok = '{"ok":true}'

        if self.path == "/api/stop":
            cur = running_appid()
            if cur != "0":
                stop_game(cur)
                wait_until_closed(5)
            return self._send(200, ok)

        if self.path == "/api/fav":
            appid = str(body.get("appid", ""))
            if find_game(appid):
                set_fav(appid, bool(body.get("fav")))
            return self._send(200, ok)

        if self.path == "/api/volume" and body.get("action") in VOLUME:
            press_key(*VOLUME[body["action"]])
            return self._send(200, ok)

        if self.path == "/api/steam" and body.get("action") in STEAM_URLS:
            os.startfile(STEAM_URLS[body["action"]])
            return self._send(200, ok)

        if self.path == "/api/pin":
            new = str(body.get("pin", ""))
            if not (new.isdigit() and 4 <= len(new) <= 12):
                return self._send(400, '{"error":"pin"}')
            with _lock:
                d = _data()
                d["pin"] = new
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/lang":
            lg = str(body.get("lang", "tr"))
            if lg not in ("tr", "en"):
                lg = "tr"
            with _lock:
                d = _data()
                d["lang"] = lg
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/clipboard":
            if "text" in body:
                set_clipboard(str(body["text"])[:5000])
                return self._send(200, ok)
            return self._send(200, json.dumps({"text": get_clipboard()}))

        if self.path == "/api/combo" and body.get("key") in COMBOS:
            press_combo(COMBOS[body["key"]])
            return self._send(200, ok)

        if self.path == "/api/open":
            g = find_game(str(body.get("appid", "")))
            if not g:
                return self._send(404, '{"error":"oyun yok"}')
            what = body.get("what")
            if what == "store":
                os.startfile(f"steam://store/{g['appid']}")
            elif what == "props":
                os.startfile(f"steam://gameproperties/{g['appid']}")
            elif what == "folder" and os.path.isdir(g["dir"]):
                os.startfile(g["dir"])
            return self._send(200, ok)

        if self.path == "/api/apps":
            act, i = body.get("action"), _int(body.get("i"))
            if act in ("add", "del"):
                with _lock:
                    d = _data()
                    apps = d.setdefault("apps", [])
                    name, path = str(body.get("name", "")).strip()[:30], str(body.get("path", "")).strip()[:300]
                    if act == "add" and name and path:
                        apps.append({"name": name, "path": path})
                    elif act == "del" and 0 <= i < len(apps):
                        apps.pop(i)
                    _save(d)
            elif act == "run":
                apps = _data().get("apps", [])
                if 0 <= i < len(apps):
                    try:
                        os.startfile(apps[i]["path"])
                    except OSError as e:
                        log(f"program açılamadı: {e}")
                        return self._send(500, '{"error":"run"}')
            return self._send(200, ok)

        if self.path == "/api/schedule":
            act, i = body.get("action"), _int(body.get("i"))
            with _lock:
                d = _data()
                sc = d.setdefault("schedules", [])
                if act == "add" and find_game(str(body.get("appid", ""))) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body.get("time", ""))):
                    sc.append({"appid": str(body["appid"]), "time": body["time"], "done": ""})
                elif act == "del" and 0 <= i < len(sc):
                    sc.pop(i)
                _save(d)
            return self._send(200, ok)

        if self.path in ("/api/note", "/api/hide"):
            appid = str(body.get("appid", ""))
            if not find_game(appid):
                return self._send(404, '{"error":"oyun yok"}')
            with _lock:
                d = _data()
                if self.path == "/api/note":
                    text = str(body.get("text", "")).strip()[:60]
                    notes = d.setdefault("notes", {})
                    if text:
                        notes[appid] = text
                    else:
                        notes.pop(appid, None)
                else:
                    hid = set(d.get("hidden", []))
                    (hid.add if body.get("hidden") else hid.discard)(appid)
                    d["hidden"] = sorted(hid)
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/settings":
            with _lock:
                d = _data()
                for key, hi in (("break", 240), ("goal", 720)):
                    if key in body:
                        try:
                            d[key] = max(0, min(hi, int(body[key])))
                        except (TypeError, ValueError):
                            pass
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/gametimer":
            try:
                m = max(0, min(720, int(body.get("minutes", 0))))
            except (TypeError, ValueError):
                return self._send(400, '{"error":"bad"}')
            if m and running_appid() == "0":
                return self._send(400, '{"error":"oyun yok"}')
            _state["kill_at"] = time.time() + m * 60 if m else 0
            return self._send(200, ok)

        if self.path == "/api/message":
            text = str(body.get("text", "")).strip()
            if text:
                show_message(text)
            return self._send(200, ok)

        if self.path == "/api/timer":
            try:
                m = max(1, min(720, int(body.get("minutes", 0))))
            except (TypeError, ValueError):
                return self._send(400, '{"error":"bad"}')
            cancel_shutdown() if body.get("cancel") else shutdown_timer(m)
            return self._send(200, ok)

        if self.path == "/api/chat":
            return self._send(200, json.dumps(chat_action(body)))

        if self.path == "/api/quiz":
            return self._send(200, json.dumps(quiz_submit(body.get("answers") or [])))

        if self.path == "/api/me":
            if body.get("action") == "claim":
                return self._send(200, json.dumps(quest_claim(str(body.get("id", "")))))
            if body.get("action") == "spin":
                return self._send(200, json.dumps(wheel_spin()))
            return self._send(200, json.dumps(prof_action(body)))

        if self.path == "/api/admin":
            return self._send(200, json.dumps(admin_action(body)))

        if self.path == "/api/guest":
            gp = str(body.get("pin", "")).strip()
            if gp and (not (gp.isdigit() and 4 <= len(gp) <= 12) or gp == get_pin()):
                return self._send(400, '{"error":"pin"}')
            with _lock:
                d = _data()
                if "pin" in body:
                    d["guest_pin"] = gp
                if "stop" in body:
                    d["guest_stop"] = bool(body["stop"])
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/audio-device":
            return self._send(200, ok) if set_audio_device(str(body.get("id", ""))) else self._send(500, '{"error":"dev"}')

        if self.path == "/api/app-volume":
            pid = _int(body.get("pid"))
            if pid < 0:
                return self._send(400, '{"error":"bad"}')
            set_app_volume(pid, body.get("vol"), body.get("mute"))
            return self._send(200, ok)

        if self.path == "/api/macros":
            act = body.get("action")
            with _lock:
                d = _data()
                ms = d.setdefault("macros", [])
                if act == "add":
                    name = str(body.get("name", "")).strip()[:24]
                    keys = str(body.get("keys", "")).strip()[:60]
                    if not name or not parse_combo(keys):
                        return self._send(400, '{"error":"keys"}')
                    ms.append({"name": name, "keys": keys})
                elif act == "del" and 0 <= _int(body.get("i")) < len(ms):
                    ms.pop(_int(body.get("i")))
                _save(d)
            if act == "run":
                return self._send(200, ok) if run_macro(_int(body.get("i"))) else self._send(400, '{"error":"run"}')
            return self._send(200, ok)

        if self.path == "/api/backup":
            act = body.get("action")
            if act == "run":
                return self._send(200, json.dumps(backup_now(body)))
            with _lock:
                d = _data()
                bs = d.setdefault("backups", [])
                if act == "add":
                    src = str(body.get("src", "")).strip()
                    name = str(body.get("name", "")).strip()[:40]
                    if not name or not src:
                        return self._send(400, '{"error":"bad"}')
                    bs.append({"name": name, "src": src, "auto": bool(body.get("auto", True)),
                               "every": max(1, min(168, _int(body.get("every")) or 6)), "keep": max(1, min(30, _int(body.get("keep")) or 5))})
                elif act == "del" and 0 <= _int(body.get("i")) < len(bs):
                    bs.pop(_int(body.get("i")))
                elif act == "toggle" and 0 <= _int(body.get("i")) < len(bs):
                    bs[_int(body.get("i"))]["auto"] = not bs[_int(body.get("i"))].get("auto")
                _save(d)
            if act == "add":
                threading.Thread(target=lambda: backup_now(bs[-1]), daemon=True).start()
            return self._send(200, ok)

        if self.path == "/api/open-folder":
            path = body.get("path") or BACKUP_DIR
            if os.path.isdir(path):
                os.startfile(path)
            return self._send(200, ok)

        if self.path == "/api/proc":
            return self._send(200, ok) if kill_proc(body.get("name", "")) else self._send(400, '{"error":"korumali"}')

        if self.path == "/api/monitor":
            act = body.get("action")
            if act == "bright":
                return self._send(200, ok) if set_brightness(str(body.get("id", "")), body.get("v", 50)) else self._send(500, '{"error":"ddc"}')
            if act == "warm":
                return self._send(200, json.dumps({"ok": True, "level": set_warmth(_int(body.get("level")))}))
            if act == "off":
                screen_off()
                return self._send(200, ok)
            if act == "nightsettings":
                os.startfile("ms-settings:nightlight")
                return self._send(200, ok)
            return self._send(400, '{"error":"bad"}')

        if self.path == "/api/storeopen":
            appid = str(body.get("appid", ""))
            if appid.isdigit():
                os.startfile(f"steam://store/{appid}")
            return self._send(200, ok)

        if self.path == "/api/gm":
            with _lock:
                d = _data()
                gm = gm_settings(d)
                for k in ("on", "power"):
                    if k in body:
                        gm[k] = bool(body[k])
                if isinstance(body.get("open"), list):
                    gm["open"] = [str(x)[:30] for x in body["open"]][:10]
                if "close" in body:
                    gm["close"] = str(body["close"])[:300]
                d["gm"] = gm
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/mouse":
            mouse(body)
            return self._send(200, ok)

        if self.path == "/api/audio-install":
            install_audio_lib()
            return self._send(200, ok)

        if self.path == "/api/steamapp":
            appid, act = str(body.get("appid", "")), body.get("action")
            urls = {"install": f"steam://install/{appid}", "uninstall": f"steam://uninstall/{appid}",
                    "validate": f"steam://validate/{appid}", "update": "steam://open/downloads"}
            if not appid.isdigit() or act not in urls:
                return self._send(400, '{"error":"bad"}')
            log(f"steam {act}: {appid}")
            os.startfile(urls[act])
            _cache["t"] = 0
            return self._send(200, ok)

        if self.path == "/api/files":
            act = body.get("action")
            if act == "open":
                os.startfile(share_dir())
                return self._send(200, ok)
            path = share_file(body.get("name", ""))
            if not path:
                return self._send(404, '{"error":"yok"}')
            if act == "del":
                os.remove(path)
                return self._send(200, ok)
            if act == "token":
                now = time.time()
                for k in [k for k, v in _dl_tokens.items() if v[1] < now]:
                    _dl_tokens.pop(k, None)
                t = secrets.token_urlsafe(16)
                _dl_tokens[t] = (os.path.basename(path), now + 600)
                return self._send(200, json.dumps({"t": t}))
            return self._send(400, '{"error":"bad"}')

        if self.path == "/api/tags":
            appid = str(body.get("appid", ""))
            if not find_game(appid) or not isinstance(body.get("tags"), list):
                return self._send(400, '{"error":"bad"}')
            clean = []
            for t in body["tags"][:8]:
                t = str(t).strip().lstrip("#").strip()[:20]
                if t and t not in clean:
                    clean.append(t)
            with _lock:
                d = _data()
                tg = d.setdefault("tags", {})
                if clean:
                    tg[appid] = clean
                else:
                    tg.pop(appid, None)
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/notify":
            topic = str(body.get("ntfy_topic", "")).strip()
            hook = str(body.get("webhook", "")).strip()
            key = str(body.get("apikey", "")).strip()
            if "ntfy_topic" in body and topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
                return self._send(400, '{"error":"topic"}')
            if "webhook" in body and hook and not WEBHOOK_RE.fullmatch(hook):
                return self._send(400, '{"error":"webhook"}')
            if "apikey" in body and key and not re.fullmatch(r"[A-Fa-f0-9]{32}", key):
                return self._send(400, '{"error":"key"}')
            with _lock:
                d = _data()
                if "ntfy_topic" in body:
                    d["ntfy_topic"] = topic
                if "webhook" in body:
                    d["webhook"] = hook
                if "apikey" in body:
                    d["apikey"] = key.upper()
                    threading.Thread(target=chat_register, daemon=True).start()
                    _web.update(t=0, data=None)
                    _fr.update(t=0, data=None)
                if isinstance(body.get("nt"), dict):
                    nt = d.setdefault("nt", {})
                    for k, v in body["nt"].items():
                        if k in NT_DEFAULT:
                            nt[k] = bool(v)
                _save(d)
            return self._send(200, ok)

        if self.path == "/api/notify-test":
            d = _data()
            try:
                if body.get("to") == "discord":
                    if not d.get("webhook"):
                        return self._send(200, '{"error":"none"}')
                    discord_send(d["webhook"], f"✅ Steam Uzaktan bağlandı. {persona()} buradan oyun duyuruları paylaşacak.")
                else:
                    if not d.get("ntfy_topic"):
                        return self._send(200, '{"error":"none"}')
                    ntfy_send(d, "Steam Uzaktan", "Test bildirimi geldi, her şey hazır.")
            except Exception as e:
                log(f"test bildirimi: {e}")
                return self._send(200, json.dumps({"error": str(e)[:80]}))
            return self._send(200, ok)

        if self.path == "/api/discord":
            act = body.get("action")
            if act == "mute":
                press_combo([0x11, 0x10, 0x4D])  # Ctrl+Shift+M
            elif act == "deafen":
                press_combo([0x11, 0x10, 0x44])  # Ctrl+Shift+D
            elif act == "open":
                try:
                    os.startfile("discord://")
                except OSError:
                    return self._send(500, '{"error":"discord"}')
            elif act == "say":
                d = _data()
                text = str(body.get("text", "")).strip()[:300]
                if not d.get("webhook"):
                    return self._send(200, '{"error":"webhook"}')
                if not text:
                    return self._send(400, '{"error":"bad"}')
                cur = running_appid()
                g = find_game(cur) if cur != "0" else None
                try:
                    discord_send(d["webhook"], f"**{persona()}:** {text}" + (f"\n🎮 Şu an **{g['name']}** oynuyor." if g else ""),
                                 g["appid"] if g else None, g["name"] if g else "")
                except Exception as e:
                    log(f"discord mesaj: {e}")
                    return self._send(200, '{"error":"send"}')
            return self._send(200, ok)

        if self.path == "/api/friendchat":
            sid = str(body.get("id", ""))
            if not re.fullmatch(r"\d{17}", sid):
                return self._send(400, '{"error":"bad"}')
            os.startfile(f"steam://friends/message/{sid}")
            return self._send(200, ok)

        if self.path == "/api/server" and body.get("action") == "stop":
            log("sunucu uzaktan kapatıldı")
            self._send(200, ok)
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return

        if self.path == "/api/power" and body.get("action") in POWER:
            cmd = POWER[body["action"]]
            log(f"güç komutu: {body['action']}")
            self._send(200, ok)
            threading.Timer(1.0, lambda: subprocess.Popen(cmd, creationflags=NO_WINDOW)).start()
            return

        if self.path == "/api/click":
            click_at(min(max(float(body.get("x", 0)), 0), 1), min(max(float(body.get("y", 0)), 0), 1),
                     bool(body.get("right")))
            return self._send(200, ok)

        if self.path == "/api/scroll":
            scroll(1 if body.get("d", 1) > 0 else -1)
            return self._send(200, ok)

        if self.path == "/api/type":
            text = str(body.get("text", ""))[:300]
            if text:
                type_text(text)
            return self._send(200, ok)

        if self.path == "/api/key" and body.get("key") in KEYS:
            press_key(KEYS[body["key"]])
            return self._send(200, ok)

        if self.path == "/api/launch":
            appid = str(body.get("appid", ""))
            quest_mark("remote")
            if not find_game(appid):
                return self._send(404, '{"error":"oyun yok"}')
            try:
                cur = running_appid()
                if cur not in ("0", appid):
                    stop_game(cur)
                    wait_until_closed(10)
                os.startfile(f"steam://rungameid/{appid}")
                return self._send(200, ok)
            except Exception as e:
                log(f"başlatma hatası: {e}")
                return self._send(500, '{"error":"launch"}')

        self._send(404, '{"error":"yok"}')


def port_busy():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def relaunch_hidden():
    """Konsoldan (python.exe / cmd) açıldıysa kendini pythonw ile penceresiz başlatır.
    Böylece CMD kapansa da sunucu çalışmaya devam eder."""
    if getattr(sys, "frozen", False) or "--fg" in sys.argv or os.name != "nt":
        return False
    exe = os.path.basename(sys.executable).lower()
    if exe.startswith("pythonw"):
        return False
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        return False
    DETACHED = 0x00000008
    NEW_GROUP = 0x00000200
    subprocess.Popen([pyw, os.path.abspath(__file__)] + sys.argv[1:],
                     creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
                     close_fds=True, cwd=_BASE)
    return True


if __name__ == "__main__":
    if "--wait" in sys.argv:
        for _ in range(40):
            if not port_busy():
                break
            time.sleep(0.5)
    if port_busy():
        print("Sunucu zaten çalışıyor. Bu pencereyi kapatabilirsin.")
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{PORT}/qr")
        except Exception:
            pass
        time.sleep(3)
        sys.exit(0)
    if relaunch_hidden():
        print("Sunucu arka planda başlatıldı. Bu pencere kapanıyor, site çalışmaya devam edecek.")
        time.sleep(2)
        sys.exit(0)
    log("sunucu başlatıldı")
    if PIN == "1234":
        print("UYARI: Varsayılan PIN kullanılıyor. Dosyanın başındaki PIN değerini değiştir.\n")
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=perf_loop, daemon=True).start()
    start_https()
    if not STANDALONE:
        threading.Thread(target=chat_loop, daemon=True).start()
        threading.Thread(target=chat_profile_daily, daemon=True).start()
        threading.Thread(target=board_publish_loop, daemon=True).start()
        threading.Thread(target=update_republish_loop, daemon=True).start()
    threading.Thread(target=event_reminder_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    _state["url"] = f"http://{tailscale_ip() or local_ip()}:{PORT}"
    print("Steam Uzaktan Başlatıcı çalışıyor.")
    print(f"Evde (Wi-Fi):        http://{local_ip()}:{PORT}")
    ts = tailscale_ip()
    if ts:
        print(f"Dışarıda (Tailscale): http://{ts}:{PORT}")
    else:
        print("Dışarıdan erişim için Tailscale kur.")
    print(f"QR kodla bağlanmak için bu PC'de aç: http://localhost:{PORT}/qr")
    print("Kapatmak için Ctrl+C (ya da sitede Ayarlar > Sunucuyu kapat)\n")
    if STANDALONE:
        def _open_welcome():
            try:
                import webbrowser
                webbrowser.open(f"http://localhost:{PORT}/qr")
            except Exception:
                pass
        threading.Timer(1.2, _open_welcome).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
