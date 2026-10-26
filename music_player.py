"""
Music Player
- UI: CustomTkinter (+ ttk.Treeview 다크/라이트 스타일)
- 재생: pygame-ce (pygame.mixer.music)
- 태그: mutagen
- 이미지: Pillow
"""
import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # 콘솔 없는 exe에서 안내 문구 출력 방지

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import pygame
from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from PIL import Image, ImageDraw, ImageTk

import tag_fixer as tf

APP_NAME = "Music Player"
DATA_DIR = Path(os.getenv("APPDATA") or Path.home()) / "ModernMusicPlayer"
DATA_FILE = DATA_DIR / "data.json"
UI_FONT = "Malgun Gothic" if sys.platform.startswith("win") else "Helvetica"
UI_FONT_BOLD = UI_FONT
FONT_FILES = {  # 프로그램에 넣어 배포하는 글꼴(SIL Open Font License 1.1, fonts/OFL-License.txt)
    "ResourceHanRoundedKR-Medium.ttf": "Resource Han Rounded KR Medium",  # 본문(중간 굵기)
    "ResourceHanRoundedKR-Bold.ttf": "Resource Han Rounded KR",           # 제목(굵게)
}


def load_bundled_fonts():
    """exe 안의 fonts 폴더에 있는 글꼴을 이 프로그램 전용으로 등록한다. 실패하면 맑은 고딕을 쓴다."""
    global UI_FONT, UI_FONT_BOLD
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "fonts"
    files = [base / f for f in FONT_FILES]
    if not all(f.is_file() for f in files):
        return False
    try:
        ok = all(ctk.FontManager.load_font(str(f)) for f in files)
    except Exception:
        ok = False
    if ok:
        UI_FONT, UI_FONT_BOLD = FONT_FILES["ResourceHanRoundedKR-Medium.ttf"], FONT_FILES["ResourceHanRoundedKR-Bold.ttf"]
    return ok
ACCENT = "#FF8000"          # 기본 강조색
ACCENT_RGB = (255, 128, 0)
ACCENT_HOVER = "#E06F00"
ACCENT_DEEP = "#C25F00"
ON_ACCENT = "#1A1A1A"        # 주황 위에 올리는 글자·아이콘 색
UNKNOWN_ARTIST = "알 수 없는 아티스트"
SORT_COLS = ("title", "artist", "genre", "album", "time")
COL_LABEL = {"title": "제목", "artist": "아티스트", "genre": "장르", "album": "앨범", "time": "시간"}
UNKNOWN_ALBUM = "알 수 없는 앨범"


# ───────────────────────── 자동 업데이트 ─────────────────────────
# app_info.json은 GitHub Actions가 빌드할 때 만들어 exe에 넣는다.
# 예: {"version": "1.0.12", "repo": "사용자이름/저장소이름"}
# 파이썬으로 직접 실행할 때는 이 파일이 없으므로 업데이트 확인을 하지 않는다.
EXE_NAME = "MusicPlayer.exe"
UPDATE_ASSET = "MusicPlayer.zip"


def app_info():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        with open(base / "app_info.json", encoding="utf-8-sig") as f:
            info = json.load(f)
        return str(info.get("version", "")), str(info.get("repo", ""))
    except (OSError, ValueError):
        return "", ""


def version_tuple(v):
    parts = []
    for piece in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "MusicPlayer-Updater",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest_release(repo):
    """GitHub 최신 릴리스를 조회한다. 반환: (태그, zip 다운로드 주소 또는 None) 또는 None."""
    with http_get(f"https://api.github.com/repos/{repo}/releases/latest") as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if not tag:
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
    return tag, asset["browser_download_url"] if asset else None


# ── 코드 업데이트 ──
# exe를 내려받아 바꾸면 백신(Windows Defender 등)이 서명 없는 exe를 지우는 경우가 있다.
# 그래서 exe는 그대로 두고 파이썬 코드 파일만 내려받아 %APPDATA%\ModernMusicPlayer\code\<버전>\에 둔다.
# exe(launcher)는 시작할 때 가장 새 코드를 불러온다.
CODE_DIR = Path(os.getenv("APPDATA") or Path.home()) / "ModernMusicPlayer" / "code"
CODE_FILES = ("music_player.py", "tag_fixer.py")
REQUIRES_LAUNCHER = 2     # 이 코드를 실행하는 데 필요한 launcher 버전(2: 글꼴 내장)
APP_VERSION = None        # launcher가 넣어 준다
LAUNCHER_VERSION = None   # launcher가 넣어 준다(구버전 exe에서는 None)


def download_code(repo, tag):
    """태그 시점의 코드 파일을 내려받아 문법을 확인한 뒤 code/<버전>에 저장한다. 필요한 launcher 버전을 돌려준다."""
    version = tag.lstrip("vV")
    CODE_DIR.mkdir(parents=True, exist_ok=True)
    part = CODE_DIR / f".{version}.part"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir()
    need = 1
    try:
        for name in CODE_FILES:
            with http_get(f"https://raw.githubusercontent.com/{repo}/{tag}/{name}", timeout=30) as r:
                data = r.read()
            compile(data, name, "exec")  # 깨진 파일이면 여기서 오류
            m = re.search(rb"^REQUIRES_LAUNCHER\s*=\s*(\d+)", data, re.M)
            if m:
                need = max(need, int(m.group(1)))
            (part / name).write_bytes(data)
    except Exception:
        shutil.rmtree(part, ignore_errors=True)
        raise
    final = CODE_DIR / version
    shutil.rmtree(final, ignore_errors=True)
    part.rename(final)
    # 오래된 코드는 최근 3개만 남긴다
    olds = sorted((d for d in CODE_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: version_tuple(d.name), reverse=True)
    for d in olds[3:]:
        shutil.rmtree(d, ignore_errors=True)
    return need


def restart_app():
    """같은 exe를 새로 실행한다(exe는 바뀌지 않으므로 백신 문제가 없다)."""
    exe = Path(sys.executable).resolve()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags, close_fds=True)


def download_and_extract(url, progress=None):
    """zip을 임시 폴더에 받아 풀고 EXE_NAME이 들어 있는 폴더 경로를 돌려준다."""
    work = Path(tempfile.mkdtemp(prefix="mmp_update_"))
    zpath = work / UPDATE_ASSET
    with http_get(url, timeout=30) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    out = work / "new"
    with zipfile.ZipFile(zpath) as z:
        root = out.resolve()
        for name in z.namelist():  # zip 밖으로 빠져나가는 경로 차단
            if not (out / name).resolve().is_relative_to(root):
                raise ValueError("잘못된 업데이트 파일입니다.")
        z.extractall(out)
    for exe in out.rglob(EXE_NAME):
        return exe.parent
    raise FileNotFoundError(f"업데이트 파일에 {EXE_NAME}가 없습니다.")


UPDATE_LOG = Path(os.getenv("APPDATA") or Path.home()) / "ModernMusicPlayer" / "update.log"


def ulog(msg):
    try:
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(UPDATE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def launch_updater(new_dir):
    """임시 폴더에 풀어 둔 새 버전 exe를 '설치 모드'로 실행한다.
    새 exe가 이 프로그램이 끝나기를 기다렸다가 자기 파일을 설치 폴더에 복사하고 다시 실행한다."""
    install_dir = Path(sys.executable).resolve().parent
    new_exe = Path(new_dir) / EXE_NAME
    ulog(f"launch helper: {new_exe} -> {install_dir} (pid {os.getpid()})")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(new_exe), "--apply-update", str(install_dir), str(os.getpid())],
                     cwd=str(new_dir), creationflags=flags, close_fds=True)


def _wait_for_exit(pid, timeout=60):
    if os.name != "nt":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, int(timeout * 1000))
        k32.CloseHandle(handle)


def run_update_helper(install_dir, old_pid):
    """설치 모드: 기존 프로그램 종료를 기다린 뒤 새 파일을 복사하고 설치된 exe를 실행한다."""
    src = Path(sys.executable).resolve().parent
    dst = Path(install_dir)
    ulog(f"helper start: {src} -> {dst}")
    _wait_for_exit(old_pid)
    time.sleep(0.5)
    last_error = None
    for attempt in range(30):  # 파일이 아직 잠겨 있으면 0.5초 간격으로 재시도
        try:
            for item in src.iterdir():
                target = dst / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            last_error = None
            break
        except OSError as e:
            last_error = e
            time.sleep(0.5)
    if last_error:
        ulog(f"copy failed: {last_error}")
        ctypes_msg(f"업데이트 파일을 복사하지 못했습니다.\n{last_error}\n\n"
                   f"직접 설치하려면 다음 폴더의 내용을 프로그램 폴더에 덮어쓰세요.\n{src}")
        return
    ulog("copy ok, relaunch")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(dst / EXE_NAME), "--updated"], cwd=str(dst), creationflags=flags, close_fds=True)


def ctypes_msg(text):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)


def cleanup_old_updates():
    """지난 업데이트가 남긴 임시 폴더를 지운다(사용 중이면 건너뜀)."""
    for d in Path(tempfile.gettempdir()).glob("mmp_update_*"):
        try:
            if Path(sys.executable).resolve().is_relative_to(d.resolve()):
                continue
        except (OSError, ValueError):
            pass
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 데이터 저장 ─────────────────────────
def load_data():
    data = {"music_folder": "", "favorites": [], "playlists": {}, "volume": 0.7, "appearance": "dark"}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data.update(json.load(f))
    except (OSError, ValueError):
        pass
    return data


def save_data(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
    except OSError:
        pass


# ───────────────────────── 태그 읽기 ─────────────────────────
def fix_text(value):
    """CP949(한국어) 태그가 Latin-1로 잘못 해석되어 깨진 경우 복구를 시도한다."""
    if not value:
        return ""
    s = str(value).strip()
    if any("\x80" <= ch <= "\xff" for ch in s) and all(ord(ch) <= 0xFF for ch in s):
        try:
            return s.encode("latin-1").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def _first(tags, key):
    try:
        v = tags.get(key)
        return fix_text(v[0]) if v else ""
    except Exception:
        return ""


def read_track(path):
    p = Path(path)
    title = artist = album = genre = ""
    length = 0.0
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            length = float(getattr(audio.info, "length", 0) or 0)
            if audio.tags:
                title = _first(audio.tags, "title")
                artist = _first(audio.tags, "artist")
                album = _first(audio.tags, "album")
                genre = _first(audio.tags, "genre")
    except Exception:
        pass
    return {
        "path": str(p),
        "file": p.stem,
        "title": title or p.stem,
        "artist": artist or UNKNOWN_ARTIST,
        "album": album or UNKNOWN_ALBUM,
        "genre": genre,
        "length": length,
        # 화면용 대체 문구가 아닌 실제 태그 값(정보 편집 창에서 사용)
        "tag": {"title": title, "artist": artist, "album": album, "genre": genre},
    }


def load_cover(path):
    """ID3 APIC 프레임에서 앨범 아트를 꺼낸다. 없으면 None."""
    try:
        pics = ID3(path).getall("APIC")
    except Exception:
        return None
    if not pics:
        return None
    pic = next((p for p in pics if p.type == 3), pics[0])  # 3 = 앞표지
    try:
        img = Image.open(BytesIO(pic.data))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def fmt_time(sec):
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def shorten(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


# ───────────────────────── 라이브러리 스캔 캐시 ─────────────────────────
SCAN_CACHE = DATA_DIR / "library_cache.json"
SCAN_CACHE_VERSION = 2  # 2: 장르·실제 태그 값 추가
_scan_lock = threading.Lock()


def load_scan_cache():
    try:
        with open(SCAN_CACHE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) and data.get("v") == SCAN_CACHE_VERSION else None
    return files if isinstance(files, dict) else {}


def save_scan_cache(files):
    with _scan_lock:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SCAN_CACHE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": SCAN_CACHE_VERSION, "files": files}, f, ensure_ascii=False)
            os.replace(tmp, SCAN_CACHE)
        except OSError:
            pass


def update_scan_cache(tracks, removed=()):
    cache = load_scan_cache()
    for p in removed:
        cache.pop(p, None)
    for t in tracks:
        try:
            st = os.stat(t["path"])
            cache[t["path"]] = {"sig": [st.st_mtime_ns, st.st_size], "track": t}
        except OSError:
            cache.pop(t["path"], None)
    save_scan_cache(cache)


# ───────────────────────── 테마 ─────────────────────────
def build_theme():
    """CustomTkinter 기본 'green' 테마의 초록색을 주황(#FF8000) 계열로 바꾼 테마 파일을 만든다."""
    src = Path(ctk.__file__).resolve().parent / "assets" / "themes" / "green.json"
    swap = {"#2CC985": ACCENT, "#2FA572": ACCENT, "#2CBE79": ACCENT,
            "#0C955A": ACCENT_HOVER, "#106A43": ACCENT_HOVER,
            "#0B6E3D": ACCENT_DEEP, "#17472E": "#A35000"}
    try:
        theme = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "green"

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return swap.get(x.upper(), x)
        return x

    theme = walk(theme)
    # 밝은 주황 위의 흰 글자는 대비가 약하고(약 2.9:1) 글자 가장자리가 번져 보여서 어두운 글자를 쓴다
    for widget in ("CTkButton", "CTkOptionMenu", "CTkSegmentedButton"):
        if widget in theme and "text_color" in theme[widget]:
            theme[widget]["text_color"] = [ON_ACCENT, ON_ACCENT]
    if "CTkCheckBox" in theme:
        theme["CTkCheckBox"]["checkmark_color"] = [ON_ACCENT, ON_ACCENT]
    for platform in (theme.get("CTkFont") or {}).values():  # 글꼴을 지정하지 않은 위젯도 같은 글꼴로
        if isinstance(platform, dict):
            platform["family"] = UI_FONT
    out = DATA_DIR / "theme_orange.json"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(theme), encoding="utf-8")
        return str(out)
    except OSError:
        return "green"


# ───────────────────────── 재생목록 내보내기 ─────────────────────────
def write_m3u(target, tracks):
    """확장 M3U를 쓴다. .m3u8은 UTF-8, .m3u는 Windows 플레이어 호환을 위해 한국어 Windows 기본
    인코딩(CP949)으로 쓰고, CP949로 쓸 수 없는 글자가 있으면 UTF-8(BOM 포함)로 쓴다.
    돌려주는 값: 디스크에 없는 파일 수."""
    lines = ["#EXTM3U"]
    missing = 0
    for t in tracks:
        path = t["path"]
        if not os.path.exists(path):
            missing += 1
        length = int(round(t.get("length") or 0)) or -1
        title, artist = t.get("title") or Path(path).stem, t.get("artist")
        label = f"{artist} - {title}" if artist and artist != UNKNOWN_ARTIST else title
        lines += [f"#EXTINF:{length},{label}", os.path.abspath(path)]
    text = "\r\n".join(lines) + "\r\n"
    if str(target).lower().endswith(".m3u8"):
        data = text.encode("utf-8")
    else:
        try:
            data = text.encode("cp949")
        except UnicodeEncodeError:
            data = b"\xef\xbb\xbf" + text.encode("utf-8")
    with open(target, "wb") as f:
        f.write(data)
    return missing


# ───────────────────────── 이미지 헬퍼 ─────────────────────────
def rounded(img, size, radius):
    img = img.convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_note(size, color):
    """겹음표(♫). 기둥 바깥선과 들보 끝이 한 면으로 이어지게 그리고, 모든 모서리를 같은 반경으로 둥글린다.
    8배로 그린 뒤 줄여 가장자리를 매끄럽게 한다."""
    from PIL import ImageFilter
    S = size * 8
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    w = S * 0.09                                    # 기둥 두께
    beam = S * 0.15                                 # 들보 두께
    heads = [(S * 0.29, S * 0.75), (S * 0.69, S * 0.67)]
    rx, ry = S * 0.14, S * 0.11
    for hx, hy in heads:                            # 머리: 살짝 기운 타원
        head = Image.new("L", (S, S), 0)
        ImageDraw.Draw(head).ellipse((hx - rx, hy - ry, hx + rx, hy + ry), fill=255)
        m.paste(255, (0, 0), head.rotate(22, center=(hx, hy), resample=Image.BICUBIC))
    xl = heads[0][0] + rx * 0.82                    # 왼쪽 기둥의 오른쪽 선
    xr = heads[1][0] + rx * 0.82                    # 오른쪽 기둥의 오른쪽 선
    yl, yr = S * 0.22, S * 0.14                     # 들보 윗선 높이(오른쪽이 높음)
    d.rectangle((xl - w, yl, xl, heads[0][1]), fill=255)
    d.rectangle((xr - w, yr, xr, heads[1][1]), fill=255)
    # 들보: 왼쪽 기둥 바깥선에서 오른쪽 기둥 바깥선까지, 끝은 세로로 반듯하게
    d.polygon([(xl - w, yl), (xr, yr), (xr, yr + beam), (xl - w, yl + beam)], fill=255)
    # 모서리 다듬기: 흐리게 한 뒤 절반 밝기에서 자르면 볼록한 모서리와 오목한 이음매가 함께 둥글어진다
    m = m.filter(ImageFilter.GaussianBlur(S * 0.022)).point(lambda v: 255 if v >= 128 else 0)
    mask = m.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), color)
    out.putalpha(mask)
    return out


def placeholder(size):
    """앨범 아트가 없을 때 쓰는 그라데이션 + 음표 이미지."""
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    top, bottom = (58, 58, 72), (22, 22, 28)
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    note = make_note(int(size * 0.56), ACCENT_RGB)
    off = (size - note.size[0]) // 2
    img.paste(note, (off, off), note)
    return img


def make_icon(kind, size, color):
    """재생 컨트롤 아이콘을 4배 해상도로 그린 뒤 축소(안티에일리어싱)."""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "play":
        d.polygon([(S * 0.30, S * 0.18), (S * 0.30, S * 0.82), (S * 0.84, S * 0.50)], fill=color)
    elif kind == "pause":
        rad = int(S * 0.04)
        d.rounded_rectangle((S * 0.24, S * 0.18, S * 0.42, S * 0.82), radius=rad, fill=color)
        d.rounded_rectangle((S * 0.58, S * 0.18, S * 0.76, S * 0.82), radius=rad, fill=color)
    elif kind == "next":
        d.polygon([(S * 0.18, S * 0.20), (S * 0.18, S * 0.80), (S * 0.66, S * 0.50)], fill=color)
        d.rectangle((S * 0.70, S * 0.20, S * 0.80, S * 0.80), fill=color)
    elif kind == "prev":
        d.polygon([(S * 0.82, S * 0.20), (S * 0.82, S * 0.80), (S * 0.34, S * 0.50)], fill=color)
        d.rectangle((S * 0.20, S * 0.20, S * 0.30, S * 0.80), fill=color)
    elif kind == "volume":
        d.rectangle((S * 0.10, S * 0.38, S * 0.28, S * 0.62), fill=color)
        d.polygon([(S * 0.28, S * 0.38), (S * 0.50, S * 0.18), (S * 0.50, S * 0.82), (S * 0.28, S * 0.62)], fill=color)
        w = int(S * 0.06)
        d.arc((S * 0.40, S * 0.32, S * 0.70, S * 0.68), -55, 55, fill=color, width=w)
        d.arc((S * 0.40, S * 0.18, S * 0.88, S * 0.82), -55, 55, fill=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


# Material Icons 'favorite' / 'favorite_border' (Google, Apache License 2.0), 24×24 좌표계
HEART_FILLED = ("M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09"
                "C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z")
HEART_OUTLINE = ("M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5"
                 "c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3z"
                 "m-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87"
                 "C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z")


def _svg_subpaths(d, steps=24):
    """M/L/H/V/C/Z(대소문자)만 쓰는 SVG 경로를 점 목록들로 바꾼다."""
    tokens = re.findall(r"[MmLlHhVvCcZz]|-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", d)
    paths, pts = [], []
    x = y = sx = sy = 0.0
    i, cmd = 0, None

    def num():
        nonlocal i
        i += 1
        return float(tokens[i - 1])

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    paths.append(pts)
                pts, x, y = [], sx, sy
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            if pts:
                paths.append(pts)
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            sx, sy, pts = x, y, [(x, y)]
            cmd = "l" if rel else "L"  # M 뒤의 추가 좌표는 L로 처리
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            pts.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            pts.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            pts.append((x, y))
        elif c == "C":
            v = [num() for _ in range(6)]
            if rel:
                v = [v[k] + (x if k % 2 == 0 else y) for k in range(6)]
            x1, y1, x2, y2, ex, ey = v
            for k in range(1, steps + 1):
                t = k / steps
                mt = 1 - t
                pts.append((mt ** 3 * x + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t ** 3 * ex,
                            mt ** 3 * y + 3 * mt * mt * t * y1 + 3 * mt * t * t * y2 + t ** 3 * ey))
            x, y = ex, ey
    if pts:
        paths.append(pts)
    return paths


def render_svg_path(d, size, color, box=24):
    """경로를 8배 크기로 칠한 뒤 줄여 매끄러운 아이콘을 만든다(짝홀 규칙으로 구멍 처리)."""
    from PIL import ImageChops
    S = size * 8
    k = S / box
    mask = Image.new("L", (S, S), 0)
    for sub in _svg_subpaths(d):
        m = Image.new("L", (S, S), 0)
        ImageDraw.Draw(m).polygon([(px * k, py * k) for px, py in sub], fill=255)
        mask = ImageChops.logical_xor(mask.convert("1"), m.convert("1")).convert("L")
    img = Image.new("RGBA", (S, S), color)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def make_heart(size, color, filled=True):
    return render_svg_path(HEART_FILLED if filled else HEART_OUTLINE, size, color)


def ctk_icon(kind, size, light_color, dark_color):
    return ctk.CTkImage(
        light_image=make_icon(kind, size * 2, light_color),
        dark_image=make_icon(kind, size * 2, dark_color),
        size=(size, size),
    )


# ───────────────────────── 도움말 아이콘(ⓘ) ─────────────────────────
def make_info_icon(size, color):
    """동그라미 안의 작은 느낌표."""
    S = size * 8
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    w = S * 0.09
    d.ellipse((w / 2, w / 2, S - w / 2, S - w / 2), outline=255, width=int(w))
    bar = S * 0.11
    d.rounded_rectangle((S / 2 - bar / 2, S * 0.24, S / 2 + bar / 2, S * 0.60), radius=bar / 2, fill=255)
    d.ellipse((S / 2 - bar * 0.65, S * 0.68, S / 2 + bar * 0.65, S * 0.68 + bar * 1.3), fill=255)
    mask = m.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), color)
    out.putalpha(mask)
    return out


class Tooltip:
    """위젯에 마우스를 올리면 설명 창을 띄운다."""

    def __init__(self, widget, text, wrap=340):
        self.widget, self.text, self.wrap = widget, text, wrap
        self.tip = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _=None):
        if self.tip or not self.text:
            return
        dark = ctk.get_appearance_mode() == "Dark"
        x, y = self.widget.winfo_pointerx() + 14, self.widget.winfo_pointery() + 16
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        border = "#3a3a3a" if dark else "#d0d0d0"
        frame = tk.Frame(tw, bg=border, padx=1, pady=1)
        frame.pack()
        tk.Label(frame, text=self.text, justify="left", wraplength=self.wrap, padx=12, pady=9,
                 bg="#262626" if dark else "#ffffff", fg="#ececec" if dark else "#1a1a1a",
                 font=(UI_FONT, 10)).pack()
        tw.update_idletasks()
        sw = tw.winfo_screenwidth()  # 화면 오른쪽 밖으로 나가지 않게
        x = min(x, sw - tw.winfo_width() - 8)
        tw.wm_geometry(f"+{x}+{y}")

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


_INFO_ICON = {}


def info_icon(parent, text, size=15):
    """ⓘ 아이콘 라벨을 만들어 돌려준다(배치는 호출한 쪽에서)."""
    if "img" not in _INFO_ICON:
        _INFO_ICON["img"] = ctk.CTkImage(light_image=make_info_icon(size * 2, "#8a8a8a"),
                                         dark_image=make_info_icon(size * 2, "#9a9a9a"), size=(size, size))
    lbl = ctk.CTkLabel(parent, text="", image=_INFO_ICON["img"], width=size + 4, cursor="question_arrow")
    Tooltip(lbl, text)
    return lbl


# ───────────────────────── 오디오 엔진 ─────────────────────────
class Player:
    def __init__(self, volume):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self.volume = volume
        self.loaded = False
        self.paused = False
        self.length = 0.0
        self.offset = 0.0
        self._started = 0.0

    def play(self, path, length, start=0.0):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(self.volume)
        pygame.mixer.music.play(start=start)
        self.loaded, self.paused = True, False
        self.length, self.offset = length, start
        self._started = time.monotonic()

    def pause(self):
        if self.loaded and not self.paused:
            pygame.mixer.music.pause()
            self.paused = True

    def resume(self):
        if self.loaded and self.paused:
            pygame.mixer.music.unpause()
            self.paused = False

    def stop(self):
        pygame.mixer.music.stop()
        self.loaded = self.paused = False
        self.offset = 0.0

    def release(self):
        """재생을 멈추고 파일 핸들을 놓는다(파일 수정·이름 변경 전에 필요)."""
        self.stop()
        try:
            pygame.mixer.music.unload()
        except (AttributeError, pygame.error):
            pass

    def seek(self, sec):
        if not self.loaded:
            return
        was_paused = self.paused
        sec = max(0.0, min(sec, max(self.length - 0.5, 0.0)))
        try:
            pygame.mixer.music.play(start=sec)
        except (NotImplementedError, pygame.error):
            return
        self.offset = sec
        self._started = time.monotonic()
        self.paused = False
        if was_paused:
            self.pause()

    def set_volume(self, v):
        self.volume = v
        pygame.mixer.music.set_volume(v)

    def position(self):
        if not self.loaded:
            return 0.0
        ms = pygame.mixer.music.get_pos()  # play() 호출 이후 경과 시간(ms)
        return self.offset + (ms / 1000 if ms > 0 else 0)

    def finished(self):
        return (
            self.loaded
            and not self.paused
            and time.monotonic() - self._started > 0.5
            and not pygame.mixer.music.get_busy()
        )


# ───────────────────────── 태그 정리 창 ─────────────────────────
BACKUP_FILE = DATA_DIR / "tag_backup.json"


class TagFixWindow(ctk.CTkToplevel):
    """라이브러리 안의 표기 차이·깨진 글자·빈 태그·파일명 형식을 점검하고, 빈 커버·앨범·장르는
    Deezer와 MusicBrainz에서 찾아 교차 확인한다. 바뀌는 내용은 종류별 열로 보여 주고 칸마다 뺄 수 있다."""

    KINDS = ("title", "artist", "album", "genre", "cover", "name")
    KIND_LABEL = {"title": "제목", "artist": "아티스트", "album": "앨범", "genre": "장르", "cover": "커버",
                  "name": "파일명"}

    def __init__(self, app, paths, scope):
        super().__init__(app)
        self.app = app
        self.paths = paths
        self.title("태그 정리")
        self.geometry("1360x780")
        self.minsize(1000, 600)
        self.transient(app)
        self.font = app.font

        self.sugs = []
        self.override = {}      # (행 번호, 종류) → True(적용) / False(제외). 사용자가 직접 바꾼 칸만 기록
        self.lib_info = {}
        self.phase = "idle"     # idle | running | done | applying
        self.stop_flag = threading.Event()
        self.progress = {"done": 0, "total": 0, "stage": "", "source": ""}
        self._imgs = []
        self._last_refresh = 0.0
        self._rows_built = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="태그 정리", font=self.font(22, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        line = ctk.CTkFrame(head, fg_color="transparent")
        line.grid(row=1, column=0, sticky="w", pady=(2, 6))
        self.status = ctk.CTkLabel(line, anchor="w", justify="left", font=self.font(12),
                                   text_color=("#555555", "#a8a8a8"), text=f"대상: {scope} {len(paths)}곡")
        self.status.pack(side="left")
        info_icon(line, "무엇을 점검하나요?\n"
                        "· 같은 아티스트·앨범이 다르게 적힌 곡 (예: IU / 아이유 / 아이유(IU))\n"
                        "· 깨진 한글, 비어 있는 제목·아티스트\n"
                        "· 고른 형식과 다른 파일명\n"
                        "· 빈 커버·앨범·장르 (온라인 검색)\n\n"
                        "'적용'을 누르기 전에는 파일이 바뀌지 않으며, 마지막 적용은 되돌릴 수 있습니다.").pack(
            side="left", padx=(6, 0))
        self.bar = ctk.CTkProgressBar(head, height=8)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, sticky="ew")
        self.start_btn = ctk.CTkButton(head, text="검사 시작", width=120, height=34, corner_radius=17,
                                       font=self.font(13, "bold"), command=self.start_or_stop)
        self.start_btn.grid(row=0, column=1, rowspan=2, padx=(12, 0))

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 2))
        ctk.CTkLabel(opts, text="파일명 형식", font=self.font(12)).pack(side="left", padx=(0, 4))
        info_icon(opts, "파일 이름을 읽고 제안할 때 쓰는 형식입니다. 고른 형식은 저장됩니다.").pack(side="left", padx=(0, 6))
        fmt = app.data.get("name_format", "title_artist")
        self.fmt_labels = {v: k for k, v in tf.NAME_FORMATS.items()}
        self.fmt_menu = ctk.CTkOptionMenu(opts, values=list(tf.NAME_FORMATS.values()), width=130, height=28,
                                          font=self.font(12), command=self.on_format)
        self.fmt_menu.set(tf.NAME_FORMATS.get(fmt, "제목_아티스트"))
        self.fmt_menu.pack(side="left")
        self.opt_online = ctk.BooleanVar(value=True)
        self.online_chk = ctk.CTkCheckBox(opts, text="온라인에서 빈 정보 찾기", variable=self.opt_online,
                                          font=self.font(12))
        self.online_chk.pack(side="left", padx=(18, 4))
        info_icon(opts, "빈 커버·앨범·장르를 Deezer에서 먼저 찾고, 확실하지 않은 것만 MusicBrainz로 대조합니다.\n\n"
                        "· 두 곳의 커버가 같은 그림이면 '높음'으로 자동 적용\n"
                        "· 서로 다르거나 한 곳에서만 찾으면 [제외] 상태로 두고 직접 고르게 합니다\n"
                        "· 장르는 내 라이브러리에 이미 있는 장르 안에서만 제안합니다\n"
                        "· 한 번 찾은 결과는 저장되어 다음 검사가 빨라집니다").pack(side="left")

        # 종류별 요약: 체크하면 그 종류 전체 적용/제외
        summary = ctk.CTkFrame(self, corner_radius=10)
        summary.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 4))
        ctk.CTkLabel(summary, text="종류별 적용", font=self.font(12, "bold")).pack(side="left", padx=(12, 4), pady=8)
        info_icon(summary, "숫자는 '적용할 칸 / 제안된 칸'입니다. 체크를 끄면 그 종류 전체가 빠집니다.\n"
                           "표에서 칸을 클릭하면 그 칸만, 맨 왼쪽 칸을 클릭하면 그 곡 전체를 넣거나 뺍니다.").pack(
            side="left", padx=(0, 10))
        self.kind_vars, self.kind_boxes = {}, {}
        for k in self.KINDS:
            var = ctk.BooleanVar(value=True)
            box = ctk.CTkCheckBox(summary, text=self.KIND_LABEL[k], variable=var, font=self.font(12),
                                  command=lambda k=k: self.toggle_kind(k))
            box.pack(side="left", padx=(0, 16))
            self.kind_vars[k], self.kind_boxes[k] = var, box

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=3, column=0, sticky="nsew", padx=20, pady=4)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_rowconfigure(0, weight=1)
        cols = ("chk", "file") + self.KINDS
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", style="Music.Treeview", selectmode="browse")
        widths = {"chk": 36, "file": 210, "title": 150, "artist": 160, "album": 170, "genre": 110, "cover": 150,
                  "name": 230}
        for c in cols:
            self.tree.heading(c, text={"chk": "", "file": "파일"}.get(c, self.KIND_LABEL.get(c, "")),
                              anchor="center" if c == "chk" else "w")
            self.tree.column(c, width=widths[c], minwidth=36, stretch=c not in ("chk",),
                             anchor="center" if c == "chk" else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb = ctk.CTkScrollbar(mid, command=self.tree.yview)
        ysb.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        xsb = ctk.CTkScrollbar(mid, orientation="horizontal", command=self.tree.xview)
        xsb.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        detail = ctk.CTkFrame(self, corner_radius=12)
        detail.grid(row=4, column=0, sticky="ew", padx=20, pady=6)
        detail.grid_columnconfigure(1, weight=1)
        self.cover_box = ctk.CTkFrame(detail, fg_color="transparent")
        self.cover_box.grid(row=0, column=0, padx=10, pady=10, sticky="n")
        self.detail_lbl = ctk.CTkLabel(detail, text="곡을 선택하면 근거와 커버 후보가 보입니다.",
                                       font=self.font(12), anchor="nw", justify="left", wraplength=900)
        self.detail_lbl.grid(row=0, column=1, sticky="nsew", padx=(4, 12), pady=12)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=5, column=0, sticky="ew", padx=20, pady=(4, 16))
        small = dict(height=34, corner_radius=17, font=self.font(12), fg_color="transparent", border_width=1,
                     border_color=("#b5b5b5", "#3a3a3a"), hover_color=("#e2e2e2", "#232323"),
                     text_color=("#1a1a1a", "#e8e8e8"))
        ctk.CTkButton(foot, text="모두 적용", width=90, command=lambda: self.set_all(True), **small).pack(side="left")
        ctk.CTkButton(foot, text="모두 제외", width=90, command=lambda: self.set_all(False),
                      **small).pack(side="left", padx=8)
        self.undo_btn = ctk.CTkButton(foot, text="마지막 정리 되돌리기", width=150, command=self.undo_last, **small)
        self.undo_btn.pack(side="left", padx=(16, 0))
        if not BACKUP_FILE.exists():
            self.undo_btn.configure(state="disabled")
        self.apply_btn = ctk.CTkButton(foot, text="적용", width=130, height=36, corner_radius=18,
                                       font=self.font(13, "bold"), state="disabled", command=self.apply)
        self.apply_btn.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.focus)

    # ───── 칸 상태 ─────
    def fmt(self):
        return self.fmt_labels.get(self.fmt_menu.get(), "title_artist")

    def on_format(self, _=None):
        self.app.data["name_format"] = self.fmt()
        save_data(self.app.data)
        self.refresh_rows()

    def cell_value(self, i, kind):
        """그 칸에 제안된 내용(없으면 None)."""
        s = self.sugs[i]
        if kind in ("title", "artist", "album", "genre"):
            return s["changes"].get(kind)
        if kind == "cover":
            return s.get("cover")
        return tf.filename_for(s, self.fmt())

    def included(self, i, kind):
        key = (i, kind)
        if key in self.override:
            return self.override[key]
        if kind == "cover":  # 확실하지 않은 커버는 기본으로 빼 둔다
            return self.sugs[i].get("cover_level") == "높음"
        return True

    def cell_text(self, i, kind):
        v = self.cell_value(i, kind)
        if not v:
            return ""
        s = self.sugs[i]
        if kind == "cover":
            n = len(s.get("cover_options") or [])
            text = f"추가 · {s.get('cover_level', '')}" + (f" · 후보 {n}개" if n > 1 else "")
        elif kind == "name":
            text = v
        else:
            old, new = v
            text = f"{old} → {new}" if old else f"+ {new}"
        return text if self.included(i, kind) else f"[제외] {text}"

    def row_kinds(self, i):
        return [k for k in self.KINDS if self.cell_value(i, k)]

    def row_mark(self, i):
        ks = self.row_kinds(i)
        on = sum(self.included(i, k) for k in ks)
        return "▣" if on == len(ks) else ("▤" if on else "□")

    def effective(self, i):
        s = self.sugs[i]
        changes = {k: v for k, v in s["changes"].items() if self.included(i, k)}
        cover = s.get("cover") if s.get("cover") and self.included(i, "cover") else None
        name = tf.filename_for(s, self.fmt()) if self.included(i, "name") else None
        return {**s, "changes": changes, "cover": cover, "new_name": name}

    # ───── 검사 ─────
    def start_or_stop(self):
        if self.phase == "running":
            self.stop_flag.set()
            self.start_btn.configure(state="disabled", text="정리 중…")
            return
        if self.phase != "idle":
            return
        self.phase = "running"
        self.start_btn.configure(text="중지")
        self.undo_btn.configure(state="disabled")
        self.online_chk.configure(state="disabled")
        library = [t["path"] for t in self.app.tracks]
        fmt, online = self.fmt(), self.opt_online.get()

        def worker():
            pr = self.progress
            pr["stage"] = "local"

            def lprog(done, total):
                pr["done"], pr["total"] = done, total

            try:
                self.sugs = tf.analyze_local(self.paths, library, fmt, progress=lprog, info=self.lib_info)
            except Exception as e:
                self.sugs, pr["error"] = [], str(e)
            pr["stage"] = "local_done"
            if online and not self.stop_flag.is_set():
                pr["stage"], pr["done"], pr["total"], pr["source"] = "online", 0, tf.count_online_groups(self.sugs), "Deezer"
                cache = load_online_cache()

                def oprog(done, total, source):
                    pr["done"], pr["total"], pr["source"] = done, total, source

                try:
                    res = tf.online_fill(self.sugs, tf.Http(), cache, self.lib_info.get("genres", {}),
                                         progress=oprog, stop=self.stop_flag)
                    pr["summary"] = res
                except Exception as e:
                    pr["error"] = str(e)
                save_online_cache(cache)
            pr["stage"] = "finished"

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        pr = self.progress
        stage = pr["stage"]
        if stage in ("local_done", "online", "finished") and not self._rows_built:
            self._rows_built = True
            self.refresh_rows()
        self.bar.set(pr["done"] / max(pr["total"], 1))
        if stage in ("", "local"):
            self.status.configure(text=f"태그 읽는 중… {pr['done']} / {pr['total']}")
        elif stage == "online":
            if pr["source"] == "MusicBrainz":
                left = (pr["total"] - pr["done"]) * 1.15
                eta = f"{int(left // 60)}분 {int(left % 60)}초" if left >= 60 else f"{int(left)}초"
                self.status.configure(text=f"MusicBrainz로 확인 중… 약 {eta} 남음 (지금 적용 가능)")
            else:
                self.status.configure(text=f"Deezer에서 찾는 중… {pr['done']} / {pr['total']}")
            if time.monotonic() - self._last_refresh > 2:
                self._last_refresh = time.monotonic()
                self.refresh_rows()
            else:
                self.update_counts()
        if stage != "finished":
            self.after(400, self._poll)
            return
        self.phase = "done"
        self.refresh_rows()
        n = sum(1 for i in range(len(self.sugs)) if self.row_kinds(i))
        stopped = " (중지됨)" if self.stop_flag.is_set() else ""
        err = f" · 오류: {pr['error']}" if pr.get("error") else ""
        self.status.configure(text=f"검사 끝{stopped}: {len(self.sugs)}곡 중 {n}곡에 수정안{err}")
        self.bar.set(1)
        self.start_btn.configure(text="검사 완료", state="disabled")
        self.undo_btn.configure(state="normal" if BACKUP_FILE.exists() else "disabled")
        self.update_counts()

    # ───── 표 ─────
    def refresh_rows(self):
        sel = self.tree.selection()
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.sugs):
            if not self.row_kinds(i):
                continue
            self.tree.insert("", "end", iid=str(i), values=(self.row_mark(i), Path(s["path"]).name)
                             + tuple(self.cell_text(i, k) for k in self.KINDS))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.tree.yview_moveto(top)
        self.update_counts()

    def update_row(self, i):
        if self.tree.exists(str(i)):
            self.tree.item(str(i), values=(self.row_mark(i), Path(self.sugs[i]["path"]).name)
                           + tuple(self.cell_text(i, k) for k in self.KINDS))

    def update_counts(self):
        rows = [i for i in range(len(self.sugs)) if self.row_kinds(i)]
        for k in self.KINDS:
            have = [i for i in rows if self.cell_value(i, k)]
            on = sum(self.included(i, k) for i in have)
            self.kind_boxes[k].configure(text=f"{self.KIND_LABEL[k]} {on}/{len(have)}",
                                         state="normal" if have else "disabled")
            if on == len(have) and have:
                self.kind_boxes[k].select()
            else:
                self.kind_boxes[k].deselect()
        n = sum(1 for i in rows if any(self.included(i, k) for k in self.row_kinds(i)))
        mb_running = self.phase == "running" and self.progress.get("source") == "MusicBrainz"
        ok = n > 0 and (self.phase == "done" or mb_running)  # Deezer가 끝나면 바로 적용 가능
        self.apply_btn.configure(text=f"적용 ({n}곡)", state="normal" if ok else "disabled")

    def toggle_kind(self, kind):
        on = self.kind_vars[kind].get()
        for i in range(len(self.sugs)):
            if self.cell_value(i, kind):
                self.override[(i, kind)] = on
        self.refresh_rows()

    def set_all(self, on):
        for i in range(len(self.sugs)):
            for k in self.row_kinds(i):
                self.override[(i, k)] = on
        self.refresh_rows()

    def on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        i = int(row)
        col = int(self.tree.identify_column(event.x)[1:]) - 1
        name = (("chk", "file") + self.KINDS)[col]
        if name == "chk":
            ks = self.row_kinds(i)
            target = not all(self.included(i, k) for k in ks)
            for k in ks:
                self.override[(i, k)] = target
        elif name in self.KINDS and self.cell_value(i, name):
            self.override[(i, name)] = not self.included(i, name)
        else:
            return
        self.update_row(i)
        self.update_counts()
        self.tree.selection_set(row)
        self.on_select()
        return "break"

    def on_select(self, _=None):
        sel = self.tree.selection()
        for w in self.cover_box.winfo_children():
            w.destroy()
        self._imgs = []
        if not sel:
            return
        i = int(sel[0])
        s = self.sugs[i]
        lines = [f"파일: {s['path']}"]
        for k in self.KINDS:
            t = self.cell_text(i, k)
            if t:
                lines.append(f"{self.KIND_LABEL[k]}:  {t}")
        lines += [f"· {n}" for n in s["notes"]]
        on = s.get("online") or {}
        if on.get("sources"):
            lines.append("· 온라인 출처: " + ", ".join(on["sources"]))
        if s.get("cover_level") == "출처 불일치":
            lines.append("· 두 출처의 커버가 서로 다릅니다. 왼쪽에서 맞는 것을 고르세요.")
        elif s.get("cover_level") == "확인 필요":
            lines.append("· 한 출처에서만 찾은 커버입니다. 맞으면 커버 칸을 클릭해 적용하세요.")
        if "genre" in s["missing"] and "genre" not in s["changes"] and on.get("sources"):
            lines.append("· 온라인 장르가 내 라이브러리의 기존 장르와 맞지 않아 장르는 제안하지 않았습니다.")
        self.detail_lbl.configure(text="\n".join(lines))
        for j, opt in enumerate(s.get("cover_options") or []):
            try:
                img = Image.open(BytesIO(opt["data"]))
            except Exception:
                continue
            chosen = j == s.get("cover_choice", 0)
            cimg = ctk.CTkImage(rounded(img, 200, 14), size=(100, 100))
            self._imgs.append(cimg)
            ctk.CTkButton(self.cover_box, image=cimg, text=f"{opt['source']} {opt['size'][0]}px" + (" (선택)" if chosen else ""),
                          compound="top", width=110, height=130, corner_radius=10, font=self.font(11),
                          fg_color=(ACCENT if chosen else "transparent"), border_width=1,
                          border_color=("#b5b5b5", "#3a3a3a"), hover_color=("#e2e2e2", "#232323"),
                          text_color=(ON_ACCENT, ON_ACCENT) if chosen else ("#1a1a1a", "#e8e8e8"),
                          command=lambda i=i, j=j: self.choose_cover(i, j)).pack(side="left", padx=4)

    def choose_cover(self, i, j):
        tf.set_cover_choice(self.sugs[i], j)
        self.override[(i, "cover")] = True
        self.update_row(i)
        self.update_counts()
        self.on_select()

    # ───── 적용 ─────
    def apply(self):
        if self.phase == "running":  # MusicBrainz 확인 중이면 멈추고, 멈춘 뒤 적용
            self.stop_flag.set()
            self.apply_btn.configure(state="disabled", text="확인 중지 중…")
            self.after(300, self._apply_when_stopped)
            return
        self._apply_now()

    def _apply_when_stopped(self):
        if self.phase == "running":
            self.after(300, self._apply_when_stopped)
            return
        self._apply_now()

    def _apply_now(self):
        items = []
        for i in range(len(self.sugs)):
            if not self.row_kinds(i):
                continue
            e = self.effective(i)
            if e["changes"] or e["cover"] or e["new_name"]:
                items.append(e)
        if not items:
            return
        if not messagebox.askyesno("태그 정리", f"{len(items)}곡의 파일을 수정합니다.\n"
                                             "'마지막 정리 되돌리기'로 이번 변경을 되돌릴 수 있습니다.\n\n계속할까요?",
                                   parent=self):
            return
        self.phase = "applying"
        self.apply_btn.configure(state="disabled")
        self.undo_btn.configure(state="disabled")
        resume = self.app.release_files([s["path"] for s in items])
        result = {"done": 0, "backups": [], "errors": [], "t0": time.monotonic(), "resume": resume}

        def one(sug):
            try:
                result["backups"].append(tf.apply(sug, use_name=bool(sug["new_name"]),
                                                  use_cover=bool(sug["cover"]),
                                                  use_fields=tuple(sug["changes"])))
            except Exception as e:
                result["errors"].append(f"{Path(sug['path']).name}: {e}")
            result["done"] += 1

        def worker():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=3) as pool:  # 파일 쓰기와 백신 검사 대기를 겹쳐 처리
                list(pool.map(one, items))
            ulog(f"tag apply: {len(items)} files in {time.monotonic() - result['t0']:.1f}s "
                 f"(errors {len(result['errors'])})")

        threading.Thread(target=worker, daemon=True).start()
        self._poll_job(result, len(items), self._apply_done)

    def _poll_job(self, result, total, on_done):
        if not self.winfo_exists():
            return
        self.bar.set(result["done"] / max(total, 1))
        self.status.configure(text=f"처리 중… {result['done']} / {total}")
        if result["done"] < total:
            self.after(200, lambda: self._poll_job(result, total, on_done))
        else:
            on_done(result)

    def _apply_done(self, result):
        if result["backups"]:
            save_backup(result["backups"])
        self.app.apply_file_changes([(b["old_path"], b["new_path"]) for b in result["backups"]],
                                    resume=result.get("resume"))
        msg = f"{len(result['backups'])}곡을 수정했습니다."
        if result["errors"]:
            msg += f"\n\n수정하지 못한 파일 {len(result['errors'])}개:\n" + "\n".join(result["errors"][:10])
        messagebox.showinfo("태그 정리", msg, parent=self)
        self.destroy()

    # ───── 되돌리기 ─────
    def undo_last(self):
        entries = load_backup()
        if not entries:
            messagebox.showinfo("태그 정리", "되돌릴 기록이 없습니다.", parent=self)
            return
        if not messagebox.askyesno("태그 정리", f"마지막으로 정리한 {len(entries)}곡을 원래대로 되돌릴까요?",
                                   parent=self):
            return
        self.phase = "applying"
        for b in (self.start_btn, self.apply_btn, self.undo_btn):
            b.configure(state="disabled")
        resume = self.app.release_files([e["new_path"] for e in entries])
        result = {"done": 0, "mapping": {}, "errors": [], "resume": resume}

        def worker():
            for e in entries:
                try:
                    result["mapping"][e["new_path"]] = tf.undo(e)
                except Exception as ex:
                    result["errors"].append(f"{Path(e['new_path']).name}: {ex}")
                result["done"] += 1

        threading.Thread(target=worker, daemon=True).start()
        self._poll_job(result, len(entries), self._undo_done)
        result.setdefault("t0", time.monotonic())

    def _undo_done(self, result):
        try:
            BACKUP_FILE.unlink()
        except OSError:
            pass
        self.app.apply_file_changes(list(result["mapping"].items()), resume=result.get("resume"))
        msg = f"{len(result['mapping'])}곡을 되돌렸습니다."
        if result["errors"]:
            msg += f"\n\n되돌리지 못한 파일 {len(result['errors'])}개:\n" + "\n".join(result["errors"][:10])
        messagebox.showinfo("태그 정리", msg, parent=self)
        self.destroy()

    def on_close(self):
        if self.phase == "applying":
            return  # 파일을 쓰는 중에는 닫지 않는다
        self.stop_flag.set()
        self.destroy()


ONLINE_CACHE = DATA_DIR / "online_cache.json"


def load_online_cache():
    try:
        with open(ONLINE_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_online_cache(cache):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ONLINE_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def save_backup(entries):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def load_backup():
    try:
        with open(BACKUP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


# ───────────────────────── 정보 편집 창 ─────────────────────────
class EditWindow(ctk.CTkToplevel):
    """선택한 곡들의 제목·아티스트·장르·앨범을 표 형태로 고친다. 저장하면 파일 태그에 기록한다."""

    FIELDS = ("title", "artist", "genre", "album")
    MAX_ROWS = 300

    def __init__(self, app, tracks):
        super().__init__(app)
        self.app = app
        self.tracks = tracks[: self.MAX_ROWS]
        self.title("정보 편집")
        self.geometry(f"1180x{min(260 + 44 * len(self.tracks), 760)}")
        self.minsize(900, 360)
        self.transient(app)
        self.font = app.font
        self.entries = []      # 행마다 {필드: CTkEntry}
        self.saving = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        ctk.CTkLabel(head, text="정보 편집", font=self.font(22, "bold")).pack(side="left")
        more = f" (처음 {self.MAX_ROWS}곡만 표시)" if len(tracks) > self.MAX_ROWS else ""
        self.status = ctk.CTkLabel(head, text=f"{len(self.tracks)}곡{more}", font=self.font(12),
                                   text_color=("#555555", "#a8a8a8"))
        self.status.pack(side="left", padx=(12, 4))
        info_icon(head, "칸을 고친 뒤 '저장'을 누르면 파일에 기록됩니다(Ctrl+S).\n"
                        "바뀐 칸은 주황 테두리로 표시됩니다. 칸을 비우고 저장하면 그 태그를 지웁니다.\n"
                        "맨 위 '모두' 줄에 입력하고 ↓를 누르면 아래 모든 곡에 같은 값이 들어갑니다.").pack(side="left")

        # 일괄 입력 줄
        bulk = ctk.CTkFrame(self, corner_radius=10)
        bulk.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 6))
        self._grid_cols(bulk)
        ctk.CTkLabel(bulk, text="모두", font=self.font(12, "bold"), anchor="w").grid(row=0, column=0, padx=(12, 6),
                                                                                    pady=8, sticky="w")
        genres = sorted({t.get("genre") for t in app.tracks if t.get("genre")}, key=str.casefold)
        self.bulk = {}
        for j, f in enumerate(self.FIELDS, start=1):
            cell = ctk.CTkFrame(bulk, fg_color="transparent")
            cell.grid(row=0, column=j, padx=4, pady=8, sticky="ew")
            cell.grid_columnconfigure(0, weight=1)
            if f == "genre":
                w = ctk.CTkComboBox(cell, values=genres or [""], height=30, font=self.font(12))
                w.set("")
            else:
                w = ctk.CTkEntry(cell, height=30, font=self.font(12),
                                 placeholder_text=f"{COL_LABEL[f]} 일괄 입력")
            w.grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(cell, text="↓", width=30, height=30, corner_radius=8, font=self.font(13, "bold"),
                          command=lambda f=f: self.fill_all(f)).grid(row=0, column=1, padx=(4, 0))
            self.bulk[f] = w

        # 곡 목록
        body = ctk.CTkScrollableFrame(self, corner_radius=10)
        body.grid(row=2, column=0, sticky="nsew", padx=20, pady=4)
        self._grid_cols(body)
        for j, label in enumerate(["파일"] + [COL_LABEL[f] for f in self.FIELDS]):
            ctk.CTkLabel(body, text=label, font=self.font(11, "bold"), text_color=("#6b6b6b", "#8f8f8f"),
                         anchor="w").grid(row=0, column=j, padx=6, pady=(4, 2), sticky="w")
        self._border = None
        for i, t in enumerate(self.tracks, start=1):
            ctk.CTkLabel(body, text=shorten(Path(t["path"]).name, 34), font=self.font(11), anchor="w",
                         text_color=("#555555", "#a8a8a8")).grid(row=i, column=0, padx=6, pady=3, sticky="w")
            row = {}
            for j, f in enumerate(self.FIELDS, start=1):
                e = ctk.CTkEntry(body, height=30, font=self.font(12))
                e.insert(0, self.original(t, f))
                e.grid(row=i, column=j, padx=4, pady=3, sticky="ew")
                if self._border is None:
                    self._border = e.cget("border_color")
                e.bind("<KeyRelease>", lambda ev, i=i - 1, f=f: self.mark(i, f))
                row[f] = e
            self.entries.append(row)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 16))
        fmt = tf.NAME_FORMATS.get(app.data.get("name_format", "title_artist"), "제목_아티스트")
        self.opt_rename = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(foot, text=f"파일명도 '{fmt}' 형식으로 맞추기", variable=self.opt_rename,
                        font=self.font(12)).pack(side="left")
        self.save_btn = ctk.CTkButton(foot, text="저장", width=120, height=36, corner_radius=18,
                                      font=self.font(13, "bold"), command=self.save)
        self.save_btn.pack(side="right")
        ctk.CTkButton(foot, text="취소", width=90, height=36, corner_radius=18, font=self.font(12),
                      fg_color="transparent", border_width=1, border_color=("#b5b5b5", "#3a3a3a"),
                      hover_color=("#e2e2e2", "#232323"), text_color=("#1a1a1a", "#e8e8e8"),
                      command=self.on_close).pack(side="right", padx=8)

        self.bind("<Control-s>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.on_close())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.focus)

    def _grid_cols(self, frame):
        for j, wgt in enumerate((0, 3, 2, 1, 2)):
            frame.grid_columnconfigure(j, weight=wgt, minsize=220 if j == 0 else 0)

    @staticmethod
    def original(t, f):
        tag = t.get("tag")
        if tag is not None:
            return tag.get(f, "") or ""
        # 예전 캐시 형식: 화면용 대체 문구는 빈 값으로
        v = t.get(f, "") or ""
        return "" if v in (UNKNOWN_ARTIST, UNKNOWN_ALBUM) else v

    def mark(self, i, f):
        e = self.entries[i][f]
        changed = e.get().strip() != self.original(self.tracks[i], f)
        e.configure(border_color=ACCENT if changed else self._border, border_width=2 if changed else 1)
        n = sum(1 for k in range(len(self.tracks)) if self.row_changes(k))
        self.status.configure(text=f"{len(self.tracks)}곡 · 바뀐 곡 {n}")

    def fill_all(self, f):
        v = self.bulk[f].get().strip()
        for i, row in enumerate(self.entries):
            row[f].delete(0, "end")
            row[f].insert(0, v)
            self.mark(i, f)

    def row_changes(self, i):
        t = self.tracks[i]
        out = {}
        for f in self.FIELDS:
            new = self.entries[i][f].get().strip()
            old = self.original(t, f)
            if new != old:
                out[f] = (old, new)
        return out

    def save(self):
        if self.saving:
            return
        fmt = self.app.data.get("name_format", "title_artist")
        items = []
        for i, t in enumerate(self.tracks):
            ch = self.row_changes(i)
            new_name = None
            if self.opt_rename.get():
                title = self.entries[i]["title"].get().strip() or Path(t["path"]).stem
                artist = self.entries[i]["artist"].get().strip()
                if artist:
                    nm = tf.build_filename(title, artist, fmt, Path(t["path"]).suffix.lower())
                    new_name = nm if nm != Path(t["path"]).name else None
            if ch or new_name:
                items.append({"path": t["path"], "changes": ch, "cover": None, "new_name": new_name})
        if not items:
            self.on_close()
            return
        self.saving = True
        self.save_btn.configure(state="disabled", text="저장 중…")
        resume = self.app.release_files([s["path"] for s in items])
        result = {"backups": [], "errors": []}

        def worker():
            for sug in items:
                try:
                    result["backups"].append(tf.apply(sug, use_name=bool(sug["new_name"]), use_cover=False,
                                                      use_fields=tuple(sug["changes"])))
                except Exception as e:
                    result["errors"].append(f"{Path(sug['path']).name}: {e}")
            self.after(0, lambda: self.done(result, resume))

        threading.Thread(target=worker, daemon=True).start()

    def done(self, result, resume):
        self.app.apply_file_changes([(b["old_path"], b["new_path"]) for b in result["backups"]], resume=resume)
        if result["errors"]:
            messagebox.showwarning("정보 편집", f"저장하지 못한 파일 {len(result['errors'])}개:\n"
                                   + "\n".join(result["errors"][:10]), parent=self)
        self.destroy()

    def on_close(self):
        if not self.saving:
            self.destroy()


# ───────────────────────── 메인 앱 ─────────────────────────
class MusicApp(ctk.CTk):
    def __init__(self):
        self.data = load_data()
        ctk.set_appearance_mode(self.data.get("appearance", "dark"))
        ctk.set_default_color_theme(build_theme())
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1320x800")
        self.minsize(1080, 660)

        try:
            self.player = Player(float(self.data.get("volume", 0.7)))
        except pygame.error as e:
            messagebox.showerror(APP_NAME, f"오디오 장치를 초기화할 수 없습니다.\n{e}")
            raise SystemExit(1)

        self.tracks, self.by_path = [], {}
        self.favorites = set(self.data.get("favorites", []))
        self.view = "all"  # "all" | "favorites" | ("playlist", 이름)
        self.view_tracks = []
        self.queue, self.queue_index, self.current = [], -1, None
        self.seeking = False
        self._search_job = None
        self._scan_state = None
        self._fail_streak = 0
        self._path_remap = {}
        self.tag_window = None
        self.pl_buttons = {}

        self.ph_big = ctk.CTkImage(rounded(placeholder(504), 504, 36), size=(252, 252))
        self.ph_small = ctk.CTkImage(rounded(placeholder(112), 112, 14), size=(56, 56))
        self.ic_play = ctk_icon("play", 22, ON_ACCENT, ON_ACCENT)
        self.ic_pause = ctk_icon("pause", 22, ON_ACCENT, ON_ACCENT)
        self.ic_prev = ctk_icon("prev", 18, "#2b2b2b", "#e6e6e6")
        self.ic_next = ctk_icon("next", 18, "#2b2b2b", "#e6e6e6")
        self.ic_vol = ctk_icon("volume", 18, "#555555", "#b3b3b3")
        self.ic_heart_on = ctk.CTkImage(make_heart(40, ACCENT), size=(20, 20))
        self.ic_heart_off = ctk.CTkImage(light_image=make_heart(40, "#555555", filled=False),
                                         dark_image=make_heart(40, "#b3b3b3", filled=False), size=(20, 20))
        self.tree_hearts = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self._build_nowplaying()
        self._build_playerbar()
        self.apply_tree_style()
        self.render_playlists()
        self.update_nav()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(250, self.tick)

        folder = self.data.get("music_folder")
        if folder and os.path.isdir(folder):
            self.start_scan(folder)
        else:
            self.view_count.configure(text="왼쪽 아래 '음악 폴더 선택'을 눌러 라이브러리를 불러오세요.")

        self.version, self.repo = app_info()
        if APP_VERSION:
            self.version = APP_VERSION
        if self.version:
            self.title(f"{APP_NAME}  v{self.version}")
        if self.version and self.repo and getattr(sys, "frozen", False):
            self.after(2000, self.check_update)

    # ───── 자동 업데이트 ─────
    def check_update(self):
        def worker():
            try:
                latest = fetch_latest_release(self.repo)
            except Exception:
                return  # 오프라인 등: 조용히 넘어간다
            if latest and version_tuple(latest[0]) > version_tuple(self.version):
                self.after(0, lambda: self.ask_update(*latest))

        threading.Thread(target=worker, daemon=True).start()

    def ask_update(self, tag, url):
        if not messagebox.askyesno(
                APP_NAME, f"새 버전이 있습니다.\n\n현재: v{self.version}\n최신: {tag}\n\n"
                          "지금 업데이트할까요? 프로그램이 잠시 닫혔다가 다시 열립니다."):
            return
        self.update_btn_state("업데이트를 내려받는 중…")

        def worker():
            try:
                need = download_code(self.repo, tag)
            except Exception as e:
                err = str(e)
                ulog(f"code download failed: {err}")
                self.after(0, lambda: messagebox.showerror(APP_NAME, f"업데이트를 내려받지 못했습니다.\n{err}"))
                self.after(0, self.refresh_list)
                return
            ulog(f"code {tag} downloaded (needs launcher {need}, have {LAUNCHER_VERSION})")
            self.after(0, lambda: self.finish_update(tag, url, need))

        threading.Thread(target=worker, daemon=True).start()

    def update_btn_state(self, text):
        self.view_count.configure(text=text)

    def finish_update(self, tag, url, need):
        if not LAUNCHER_VERSION or need > LAUNCHER_VERSION:
            # 이번 업데이트는 exe 자체를 바꿔야 한다(처음 한 번, 또는 새 라이브러리가 필요할 때)
            messagebox.showinfo(
                APP_NAME, f"이번 업데이트({tag})는 프로그램 파일 전체를 새로 받아야 합니다.\n\n"
                          "확인을 누르면 다운로드 페이지가 열립니다. MusicPlayer.zip을 받아 압축을 풀고 "
                          "기존 폴더에 덮어쓴 뒤 실행하세요. 이후 업데이트는 자동으로 됩니다.")
            import webbrowser
            webbrowser.open(f"https://github.com/{self.repo}/releases/latest")
            self.refresh_list()
            return
        try:
            restart_app()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"프로그램을 다시 열지 못했습니다. 직접 다시 실행해 주세요.\n{e}")
        self.on_close()

    def font(self, size, weight="normal"):
        if weight == "bold":  # 굵은 글자는 Bold 글꼴 파일로(가짜 굵게 처리 방지)
            return ctk.CTkFont(family=UI_FONT_BOLD, size=size, weight="bold")
        return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)

    # ───── 레이아웃 ─────
    def _nav_button(self, parent, text, cmd):
        return ctk.CTkButton(
            parent, text=text, command=cmd, anchor="w", height=38, corner_radius=8,
            fg_color="transparent", hover_color=("#e2e2e2", "#232323"),
            text_color=("#1a1a1a", "#e8e8e8"), font=self.font(14),
        )

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=("#f3f3f3", "#101010"))
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(4, weight=1)

        self.ic_logo = ctk.CTkImage(make_note(56, ACCENT), size=(26, 26))
        ctk.CTkLabel(sb, text="  Music", image=self.ic_logo, compound="left", font=self.font(22, "bold"),
                     anchor="w").grid(
            row=0, column=0, padx=22, pady=(26, 18), sticky="ew")
        self.nav_all = self._nav_button(sb, "모든 곡", lambda: self.set_view("all"))
        self.nav_all.grid(row=1, column=0, padx=12, pady=2, sticky="ew")
        self.nav_fav = self._nav_button(sb, "즐겨찾기", lambda: self.set_view("favorites"))
        self.nav_fav.grid(row=2, column=0, padx=12, pady=2, sticky="ew")

        head = ctk.CTkFrame(sb, fg_color="transparent")
        head.grid(row=3, column=0, padx=12, pady=(22, 4), sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        ttl = ctk.CTkFrame(head, fg_color="transparent")
        ttl.grid(row=0, column=0, padx=10, sticky="w")
        ctk.CTkLabel(ttl, text="플레이리스트", font=self.font(12, "bold"),
                     text_color=("#6b6b6b", "#8a8a8a"), anchor="w").pack(side="left")
        info_icon(ttl, "+ 로 새로 만들고, 곡을 우클릭해 추가합니다.\n"
                       "플레이리스트를 우클릭하면 M3U 파일로 내보내거나 삭제할 수 있습니다.", size=13).pack(
            side="left", padx=(4, 0))
        ctk.CTkButton(head, text="+", width=28, height=28, corner_radius=14, font=self.font(16),
                      fg_color="transparent", hover_color=("#e2e2e2", "#232323"),
                      text_color=("#1a1a1a", "#e8e8e8"),
                      command=lambda: self.new_playlist([])).grid(row=0, column=1)

        self.pl_frame = ctk.CTkScrollableFrame(sb, fg_color="transparent")
        self.pl_frame.grid(row=4, column=0, padx=4, pady=0, sticky="nsew")
        self.pl_frame.grid_columnconfigure(0, weight=1)

        self.folder_label = ctk.CTkLabel(sb, text=shorten(self.data.get("music_folder") or "폴더 미지정", 30),
                                         font=self.font(11), text_color=("#6b6b6b", "#8a8a8a"), anchor="w")
        self.folder_label.grid(row=5, column=0, padx=22, pady=(8, 2), sticky="ew")
        ctk.CTkButton(sb, text="음악 폴더 선택", height=36, corner_radius=18, font=self.font(13, "bold"),
                      command=self.choose_folder).grid(row=6, column=0, padx=18, pady=(2, 6), sticky="ew")
        ctk.CTkButton(sb, text="태그 정리", height=36, corner_radius=18, font=self.font(13, "bold"),
                      fg_color="transparent", border_width=1, border_color=("#b5b5b5", "#3a3a3a"),
                      hover_color=("#e2e2e2", "#232323"), text_color=("#1a1a1a", "#e8e8e8"),
                      command=self.open_tag_fixer)
        self.tag_btn = sb.winfo_children()[-1]
        self.tag_btn.grid(row=7, column=0, padx=18, pady=(0, 10), sticky="ew")
        Tooltip(self.tag_btn, "제목·아티스트·앨범 표기, 빈 커버·장르, 파일명을 점검해 고칠 내용을 보여 줍니다.\n"
                              "목록에서 곡을 2개 이상 골라 두면 그 곡들만 점검합니다.")

        self.theme_switch = ctk.CTkSwitch(sb, text="다크 모드", font=self.font(12), command=self.toggle_theme)
        self.theme_switch.grid(row=8, column=0, padx=22, pady=(0, 20), sticky="w")
        if ctk.get_appearance_mode() == "Dark":
            self.theme_switch.select()

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=0, fg_color=("#ffffff", "#181818"))
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(main, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        top.grid_columnconfigure(0, weight=1)
        self.view_title = ctk.CTkLabel(top, text="모든 곡", font=self.font(28, "bold"), anchor="w")
        self.view_title.grid(row=0, column=0, sticky="w")
        self.view_count = ctk.CTkLabel(top, text="", font=self.font(13), text_color=("#666666", "#9a9a9a"), anchor="w")
        self.view_count.grid(row=1, column=0, sticky="w")
        self.search = ctk.CTkEntry(top, width=300, height=38, corner_radius=19, border_width=1,
                                   placeholder_text="제목, 아티스트, 앨범, 파일명 검색", font=self.font(13))
        self.search.grid(row=0, column=1, rowspan=2, sticky="e")
        self.search.bind("<KeyRelease>", self.on_search)

        holder = ctk.CTkFrame(main, fg_color="transparent")
        holder.grid(row=1, column=0, sticky="nsew", padx=(28, 12), pady=(8, 16))
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)

        cols = SORT_COLS
        self.tree = ttk.Treeview(holder, columns=cols, show=("tree", "headings"), style="Music.Treeview",
                                 selectmode="extended")
        self.tree.heading("#0", text="")
        for c in cols:
            self.tree.heading(c, text=COL_LABEL[c], anchor="e" if c == "time" else "w",
                              command=lambda c=c: self.on_sort(c))
        self.update_sort_headings()
        self.tree.grid(row=0, column=0, sticky="nsew")
        sbar = ctk.CTkScrollbar(holder, command=self.tree.yview)
        sbar.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.tree.configure(yscrollcommand=sbar.set)

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Return>", lambda e: self._play_selection())

    def _build_nowplaying(self):
        np_ = ctk.CTkFrame(self, width=300, corner_radius=0, fg_color=("#f7f7f7", "#121212"))
        np_.grid(row=0, column=2, sticky="nse")
        np_.grid_propagate(False)
        np_.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(np_, text="지금 재생 중", font=self.font(15, "bold"), anchor="w").grid(
            row=0, column=0, padx=24, pady=(26, 16), sticky="ew")
        self.big_art = ctk.CTkLabel(np_, text="", image=self.ph_big)
        self.big_art.grid(row=1, column=0, padx=24)
        self.np_title = ctk.CTkLabel(np_, text="재생 중인 곡 없음", font=self.font(18, "bold"),
                                     anchor="w", justify="left", wraplength=250)
        self.np_title.grid(row=2, column=0, padx=24, pady=(18, 2), sticky="ew")
        self.np_artist = ctk.CTkLabel(np_, text="", font=self.font(13), text_color=("#555555", "#b3b3b3"),
                                      anchor="w", justify="left", wraplength=250)
        self.np_artist.grid(row=3, column=0, padx=24, sticky="ew")
        self.np_album = ctk.CTkLabel(np_, text="", font=self.font(12), text_color=("#777777", "#8a8a8a"),
                                     anchor="w", justify="left", wraplength=250)
        self.np_album.grid(row=4, column=0, padx=24, pady=(2, 0), sticky="ew")

    def _build_playerbar(self):
        bar = ctk.CTkFrame(self, height=96, corner_radius=0, fg_color=("#ececec", "#0b0b0b"))
        bar.grid(row=1, column=0, columnspan=3, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(0, weight=1)
        bar.grid_columnconfigure(0, weight=1, uniform="side")
        bar.grid_columnconfigure(1, weight=2)
        bar.grid_columnconfigure(2, weight=1, uniform="side")

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=18)
        self.small_art = ctk.CTkLabel(left, text="", image=self.ph_small)
        self.small_art.grid(row=0, column=0, rowspan=2)
        self.bar_title = ctk.CTkLabel(left, text="—", font=self.font(14, "bold"), anchor="w", width=200)
        self.bar_title.grid(row=0, column=1, padx=(12, 4), sticky="sw")
        self.bar_artist = ctk.CTkLabel(left, text="", font=self.font(12), anchor="w", width=200,
                                       text_color=("#555555", "#b3b3b3"))
        self.bar_artist.grid(row=1, column=1, padx=(12, 4), sticky="nw")
        self.fav_btn = ctk.CTkButton(left, text="", image=self.ic_heart_off, width=36, height=36, corner_radius=18,
                                     fg_color="transparent", hover_color=("#dcdcdc", "#222222"),
                                     command=self.toggle_current_fav)
        self.fav_btn.grid(row=0, column=2, rowspan=2)

        center = ctk.CTkFrame(bar, fg_color="transparent")
        center.grid(row=0, column=1, sticky="ew", padx=8)
        center.grid_columnconfigure(1, weight=1)
        btns = ctk.CTkFrame(center, fg_color="transparent")
        btns.grid(row=0, column=0, columnspan=3, pady=(8, 0))
        ghost = dict(fg_color="transparent", hover_color=("#dcdcdc", "#222222"), text="")
        ctk.CTkButton(btns, image=self.ic_prev, width=40, height=40, corner_radius=20,
                      command=self.prev_track, **ghost).pack(side="left", padx=8)
        self.play_btn = ctk.CTkButton(btns, image=self.ic_play, text="", width=46, height=46,
                                      corner_radius=23, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=8)
        ctk.CTkButton(btns, image=self.ic_next, width=40, height=40, corner_radius=20,
                      command=self.next_track, **ghost).pack(side="left", padx=8)

        self.time_now = ctk.CTkLabel(center, text="0:00", width=44, font=self.font(11),
                                     text_color=("#555555", "#a0a0a0"))
        self.time_now.grid(row=1, column=0)
        self.seek = ctk.CTkSlider(center, from_=0, to=1, height=16, command=self.on_seek_drag)
        self.seek.set(0)
        self.seek.grid(row=1, column=1, sticky="ew", padx=6)
        self.seek.bind("<Button-1>", self.on_seek_press)
        self.seek.bind("<ButtonRelease-1>", self.on_seek_release)
        self.time_total = ctk.CTkLabel(center, text="0:00", width=44, font=self.font(11),
                                       text_color=("#555555", "#a0a0a0"))
        self.time_total.grid(row=1, column=2)

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=24)
        ctk.CTkLabel(right, text="", image=self.ic_vol).pack(side="left", padx=(0, 6))
        self.vol = ctk.CTkSlider(right, from_=0, to=1, width=130, command=self.on_volume)
        self.vol.set(self.player.volume)
        self.vol.pack(side="left")

    def apply_tree_style(self):
        dark = ctk.get_appearance_mode() == "Dark"
        bg = "#181818" if dark else "#ffffff"
        fg = "#e8e8e8" if dark else "#1a1a1a"
        sub = "#8f8f8f" if dark else "#6b6b6b"
        sel = "#3a2a18" if dark else "#ffe6cc"
        scale = ctk.ScalingTracker.get_window_scaling(self)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Music.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=int(36 * scale), borderwidth=0, font=(UI_FONT, 10))
        style.configure("Music.Treeview.Heading", background=bg, foreground=sub, borderwidth=0,
                        relief="flat", font=(UI_FONT_BOLD, 9, "bold"), padding=(6, 8))
        style.map("Music.Treeview", background=[("selected", sel)], foreground=[("selected", fg)])
        style.map("Music.Treeview.Heading", background=[("active", bg)], foreground=[("active", fg)])
        style.layout("Music.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        # 하트 칸: 펼침 화살표 자리를 없애고 이미지만 가운데에 둔다
        style.layout("Music.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        style.configure("Music.Treeview", indent=0)
        hs = max(14, int(18 * scale))
        style.configure("Music.Treeview.Item", padding=(max(0, (int(44 * scale) - hs) // 2), 0, 0, 0))
        self.tree_hearts = {
            True: ImageTk.PhotoImage(make_heart(hs, ACCENT), master=self),
            False: ImageTk.PhotoImage(make_heart(hs, "#6f6f6f" if dark else "#a0a0a0", filled=False), master=self),
        }
        self.tree.column("#0", width=int(44 * scale), minwidth=int(44 * scale), stretch=False, anchor="center")
        widths = {"title": 280, "artist": 180, "genre": 110, "album": 200, "time": 64}
        for c, w in widths.items():
            self.tree.column(c, width=int(w * scale), minwidth=int(min(w, 60) * scale),
                             stretch=c != "time", anchor="e" if c == "time" else "w")
        self.tree.tag_configure("playing", foreground=ACCENT)
        if self.view_tracks:
            self.update_fav_cells()

    # ───── 스캔 ─────
    def choose_folder(self):
        folder = filedialog.askdirectory(title="음악 폴더 선택",
                                         initialdir=self.data.get("music_folder") or str(Path.home()))
        if folder:
            self.data["music_folder"] = folder
            save_data(self.data)
            self.folder_label.configure(text=shorten(folder, 30))
            self.start_scan(folder)

    def start_scan(self, folder):
        if self._scan_state and not self._scan_state["done"]:
            return
        state = {"done": False, "count": 0, "total": 0, "result": []}
        self._scan_state = state

        def worker():
            t0 = time.monotonic()
            files = []
            for root, _, names in os.walk(folder):
                files += [os.path.join(root, n) for n in names if n.lower().endswith(".mp3")]
            state["total"] = len(files)
            cache = load_scan_cache()
            result, reread = [], 0
            for i, f in enumerate(files, 1):
                try:
                    st = os.stat(f)
                    sig = [st.st_mtime_ns, st.st_size]
                except OSError:
                    continue
                hit = cache.get(f)
                if hit and hit.get("sig") == sig:  # 바뀌지 않은 파일은 태그를 다시 읽지 않는다
                    result.append(hit["track"])
                else:
                    t = read_track(f)
                    result.append(t)
                    cache[f] = {"sig": sig, "track": t}
                    reread += 1
                state["count"] = i
            alive = set(files)
            save_scan_cache({k: v for k, v in cache.items() if k in alive})
            result.sort(key=lambda t: (t["artist"].casefold(), t["album"].casefold(), t["title"].casefold()))
            state["result"] = result
            state["done"] = True
            ulog(f"scan: {len(files)} files, {reread} read, {time.monotonic() - t0:.2f}s")

        threading.Thread(target=worker, daemon=True).start()
        self.view_count.configure(text="폴더를 스캔하는 중…")
        self.after(100, self._poll_scan)

    def _poll_scan(self):
        st = self._scan_state
        if not st["done"]:
            self.view_count.configure(text=f"스캔 중… {st['count']} / {st['total']}")
            self.after(100, self._poll_scan)
            return
        self.tracks = st["result"]
        self.by_path = {t["path"]: t for t in self.tracks}
        if self.queue:  # 파일 이름이 바뀌었을 수 있으므로 대기열을 새 목록에 맞춘다
            remap = self._path_remap
            cur = self.queue[self.queue_index]["path"] if 0 <= self.queue_index < len(self.queue) else None
            self.queue = [self.by_path[remap.get(t["path"], t["path"])] for t in self.queue
                          if remap.get(t["path"], t["path"]) in self.by_path]
            new_cur = remap.get(cur, cur)
            self.queue_index = next((i for i, t in enumerate(self.queue) if t["path"] == new_cur), 0)
            if self.current:
                self.current = self.by_path.get(remap.get(self.current["path"], self.current["path"]))
        self._path_remap = {}
        self.refresh_list()

    # ───── 목록 ─────
    def set_view(self, view):
        self.view = view
        self.update_nav()
        self.refresh_list()

    def update_nav(self):
        active = ("#e2e2e2", "#262626")
        self.nav_all.configure(fg_color=active if self.view == "all" else "transparent")
        self.nav_fav.configure(fg_color=active if self.view == "favorites" else "transparent")
        for name, btn in self.pl_buttons.items():
            btn.configure(fg_color=active if self.view == ("playlist", name) else "transparent")

    def base_tracks(self):
        if self.view == "all":
            return self.tracks
        if self.view == "favorites":
            return [t for t in self.tracks if t["path"] in self.favorites]
        paths = self.data["playlists"].get(self.view[1], [])
        return [self.by_path[p] for p in paths if p in self.by_path]

    def on_search(self, _=None):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(150, self.refresh_list)

    def refresh_list(self):
        self._search_job = None
        q = self.search.get().strip().casefold()
        items = self.base_tracks()
        if q:
            items = [t for t in items if q in t["title"].casefold() or q in t["artist"].casefold()
                     or q in t["album"].casefold() or q in t["file"].casefold()
                     or q in t.get("genre", "").casefold()]
        items = self.sorted_tracks(items)
        self.view_tracks = items
        self.tree.delete(*self.tree.get_children())
        cur = self.current["path"] if self.current else None
        for i, t in enumerate(items):
            self.tree.insert("", "end", iid=str(i), tags=("playing",) if t["path"] == cur else (),
                             image=self.tree_hearts[t["path"] in self.favorites],
                             values=(t["title"], t["artist"], t.get("genre", ""), t["album"], fmt_time(t["length"])))
        names = {"all": "모든 곡", "favorites": "즐겨찾기"}
        self.view_title.configure(text=names.get(self.view) if isinstance(self.view, str) else self.view[1])
        total = len(self.base_tracks())
        self.view_count.configure(text=f"검색 결과 {len(items)}곡 / 전체 {total}곡" if q else f"{total}곡")

    # ───── 정렬 ─────
    def on_sort(self, col):
        """같은 열을 누를 때마다 오름차순 → 내림차순 → 원래 순서로 바뀐다."""
        cur = self.data.get("sort") or {}
        if cur.get("col") != col:
            new = {"col": col, "desc": False}
        elif not cur.get("desc"):
            new = {"col": col, "desc": True}
        else:
            new = {}
        self.data["sort"] = new
        save_data(self.data)
        self.update_sort_headings()
        self.refresh_list()

    def update_sort_headings(self):
        cur = self.data.get("sort") or {}
        for c in SORT_COLS:
            arrow = (" ▼" if cur.get("desc") else " ▲") if cur.get("col") == c else ""
            self.tree.heading(c, text=COL_LABEL[c] + arrow)

    def sorted_tracks(self, items):
        cur = self.data.get("sort") or {}
        col = cur.get("col")
        if col not in SORT_COLS:
            return list(items)  # 원래 순서(라이브러리 기본 순서 또는 플레이리스트 순서)
        if col == "time":
            key = lambda t: t["length"]
        else:
            unknown = {"artist": UNKNOWN_ARTIST, "album": UNKNOWN_ALBUM}.get(col)

            def key(t):
                v = t.get(col) or ""
                empty = not v or v == unknown  # 비어 있는 값은 순서와 관계없이 맨 뒤
                return (empty if not cur.get("desc") else not empty, v.casefold(), t["title"].casefold())
        return sorted(items, key=key, reverse=bool(cur.get("desc")))

    def mark_playing(self):
        cur = self.current["path"] if self.current else None
        for iid in self.tree.get_children():
            t = self.view_tracks[int(iid)]
            self.tree.item(iid, tags=("playing",) if t["path"] == cur else ())

    def update_fav_cells(self):
        for iid in self.tree.get_children():
            t = self.view_tracks[int(iid)]
            self.tree.item(iid, image=self.tree_hearts[t["path"] in self.favorites])

    # ───── 목록 이벤트 ─────
    def on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) != "#0":
            self.play_from_view(int(row))

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) in ("tree", "cell") and self.tree.identify_column(event.x) == "#0":
            row = self.tree.identify_row(event.y)
            if row:
                self.toggle_fav([self.view_tracks[int(row)]["path"]])
                return "break"

    def on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        paths = [self.view_tracks[int(i)]["path"] for i in self.tree.selection()]
        all_fav = all(p in self.favorites for p in paths)

        menu = tk.Menu(self, tearoff=0, font=(UI_FONT, 10))
        menu.add_command(label="재생", command=lambda: self.play_from_view(int(row)))
        menu.add_command(label="정보 편집…" + (f" ({len(paths)}곡)" if len(paths) > 1 else ""),
                         command=lambda: self.open_editor(paths))
        menu.add_separator()
        menu.add_command(label="즐겨찾기 해제" if all_fav else "즐겨찾기에 추가",
                         command=lambda: self.toggle_fav(paths, force=not all_fav))
        sub = tk.Menu(menu, tearoff=0, font=(UI_FONT, 10))
        for name in self.data["playlists"]:
            sub.add_command(label=name, command=lambda n=name: self.add_to_playlist(n, paths))
        if self.data["playlists"]:
            sub.add_separator()
        sub.add_command(label="새 플레이리스트…", command=lambda: self.new_playlist(paths))
        menu.add_cascade(label="플레이리스트에 추가", menu=sub)
        if isinstance(self.view, tuple):
            menu.add_separator()
            menu.add_command(label="이 플레이리스트에서 제거",
                             command=lambda: self.remove_from_playlist(self.view[1], paths))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ───── 즐겨찾기 / 플레이리스트 ─────
    def toggle_fav(self, paths, force=None):
        add = force if force is not None else paths[0] not in self.favorites
        for p in paths:
            (self.favorites.add if add else self.favorites.discard)(p)
        self.data["favorites"] = sorted(self.favorites)
        save_data(self.data)
        if self.view == "favorites":
            self.refresh_list()
        else:
            self.update_fav_cells()
        self.update_fav_btn()

    def toggle_current_fav(self):
        if self.current:
            self.toggle_fav([self.current["path"]])

    def update_fav_btn(self):
        on = bool(self.current and self.current["path"] in self.favorites)
        self.fav_btn.configure(image=self.ic_heart_on if on else self.ic_heart_off)

    def render_playlists(self):
        for w in self.pl_frame.winfo_children():
            w.destroy()
        self.pl_buttons = {}
        for i, name in enumerate(self.data["playlists"]):
            btn = self._nav_button(self.pl_frame, shorten(name, 22),
                                   lambda n=name: self.set_view(("playlist", n)))
            btn.grid(row=i, column=0, padx=4, pady=1, sticky="ew")
            btn.bind("<Button-3>", lambda e, n=name: self._playlist_menu(e, n))
            self.pl_buttons[name] = btn
        self.update_nav()

    def _playlist_menu(self, event, name):
        menu = tk.Menu(self, tearoff=0, font=(UI_FONT, 10))
        menu.add_command(label="M3U 파일로 내보내기…", command=lambda: self.export_playlist(name))
        menu.add_separator()
        menu.add_command(label="플레이리스트 삭제", command=lambda: self.delete_playlist(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def new_playlist(self, paths):
        dlg = ctk.CTkInputDialog(text="플레이리스트 이름을 입력하세요.", title="새 플레이리스트")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        if name in self.data["playlists"]:
            messagebox.showwarning(APP_NAME, "같은 이름의 플레이리스트가 이미 있습니다.")
            return
        self.data["playlists"][name] = []
        self.render_playlists()
        if paths:
            self.add_to_playlist(name, paths)
        else:
            save_data(self.data)

    def add_to_playlist(self, name, paths):
        pl = self.data["playlists"].setdefault(name, [])
        pl += [p for p in paths if p not in pl]
        save_data(self.data)
        if self.view == ("playlist", name):
            self.refresh_list()

    def remove_from_playlist(self, name, paths):
        rm = set(paths)
        self.data["playlists"][name] = [p for p in self.data["playlists"].get(name, []) if p not in rm]
        save_data(self.data)
        self.refresh_list()

    def export_playlist(self, name):
        paths = self.data["playlists"].get(name, [])
        if not paths:
            messagebox.showinfo(APP_NAME, f"'{name}' 플레이리스트가 비어 있습니다.")
            return
        initial = self.data.get("export_dir") or self.data.get("music_folder") or str(Path.home())
        target = filedialog.asksaveasfilename(
            title="M3U로 내보내기", initialdir=initial, initialfile=tf.safe_filename(name) + ".m3u",
            defaultextension=".m3u",
            filetypes=[("M3U 재생목록", "*.m3u"), ("M3U8 재생목록 (UTF-8)", "*.m3u8")])
        if not target:
            return
        try:
            missing = write_m3u(target, [self.by_path.get(p) or {"path": p} for p in paths])
        except OSError as e:
            messagebox.showerror(APP_NAME, f"저장하지 못했습니다.\n{e}")
            return
        self.data["export_dir"] = str(Path(target).parent)
        save_data(self.data)
        msg = f"{len(paths)}곡을 내보냈습니다.\n{target}"
        if missing:
            msg += f"\n\n지금 폴더에 없는 파일 {missing}개도 경로 그대로 넣었습니다."
        messagebox.showinfo(APP_NAME, msg)

    def delete_playlist(self, name):
        if not messagebox.askyesno(APP_NAME, f"'{name}' 플레이리스트를 삭제할까요?"):
            return
        self.data["playlists"].pop(name, None)
        save_data(self.data)
        if self.view == ("playlist", name):
            self.view = "all"
            self.refresh_list()
        self.render_playlists()

    # ───── 재생 제어 ─────
    def _play_selection(self):
        sel = self.tree.selection()
        if sel:
            self.play_from_view(int(sel[0]))

    def play_from_view(self, idx):
        if not self.view_tracks:
            return
        self.queue = list(self.view_tracks)
        self.queue_index = idx
        self._fail_streak = 0
        self.play_current()

    def play_current(self):
        t = self.queue[self.queue_index]
        try:
            self.player.play(t["path"], t["length"])
            self._fail_streak = 0
        except pygame.error:
            self._fail_streak += 1
            self.view_count.configure(text=f"재생할 수 없는 파일을 건너뜀: {t['file']}")
            if self._fail_streak < len(self.queue):
                self.queue_index = (self.queue_index + 1) % len(self.queue)
                self.after(10, self.play_current)
            else:
                self.player.stop()
                self.update_play_icon()
            return
        self.current = t
        self.update_now_playing()
        self.mark_playing()

    def toggle_play(self):
        if not self.player.loaded:
            if self.tree.selection():
                self._play_selection()
            elif self.queue:
                self.play_current()
            elif self.view_tracks:
                self.play_from_view(0)
            return
        if self.player.paused:
            self.player.resume()
        else:
            self.player.pause()
        self.update_play_icon()

    def next_track(self):
        if self.queue:
            self.queue_index = (self.queue_index + 1) % len(self.queue)
            self.play_current()

    def prev_track(self):
        if not self.queue:
            return
        if self.player.loaded and self.player.position() > 3:
            self.player.seek(0)
            return
        self.queue_index = (self.queue_index - 1) % len(self.queue)
        self.play_current()

    def on_track_end(self):
        if self.queue_index < len(self.queue) - 1:
            self.queue_index += 1
            self.play_current()
        else:  # 대기열 끝: 정지하고 처음으로 되돌림
            self.player.stop()
            self.queue_index = 0
            self.seek.set(0)
            self.time_now.configure(text="0:00")
            self.update_play_icon()

    def on_seek_press(self, _):
        self.seeking = True

    def on_seek_drag(self, value):
        if self.player.loaded:
            self.time_now.configure(text=fmt_time(value * self.player.length))

    def on_seek_release(self, _):
        if self.player.loaded and self.player.length > 0:
            self.player.seek(self.seek.get() * self.player.length)
        self.seeking = False

    def on_volume(self, v):
        self.player.set_volume(float(v))
        self.data["volume"] = round(float(v), 3)

    def update_play_icon(self):
        playing = self.player.loaded and not self.player.paused
        self.play_btn.configure(image=self.ic_pause if playing else self.ic_play)

    def update_now_playing(self):
        t = self.current
        cover = load_cover(t["path"])
        if cover:
            self._big_img = ctk.CTkImage(rounded(cover, 504, 36), size=(252, 252))
            self._small_img = ctk.CTkImage(rounded(cover, 112, 14), size=(56, 56))
        else:
            self._big_img, self._small_img = self.ph_big, self.ph_small
        self.big_art.configure(image=self._big_img)
        self.small_art.configure(image=self._small_img)
        self.np_title.configure(text=t["title"])
        self.np_artist.configure(text=t["artist"])
        self.np_album.configure(text=t["album"])
        self.bar_title.configure(text=shorten(t["title"], 24))
        self.bar_artist.configure(text=shorten(t["artist"], 28))
        self.time_total.configure(text=fmt_time(t["length"]))
        self.title(f"{t['title']} - {t['artist']}")
        self.update_play_icon()
        self.update_fav_btn()

    def tick(self):
        p = self.player
        if p.loaded:
            if p.finished():
                self.on_track_end()
            elif not self.seeking and p.length > 0:
                pos = min(p.position(), p.length)
                self.seek.set(pos / p.length)
                self.time_now.configure(text=fmt_time(pos))
        elif not self.seeking and self.seek.get() != 0:
            self.seek.set(0)
        self.after(250, self.tick)

    # ───── 태그 정리 연동 ─────
    def open_tag_fixer(self):
        if not self.tracks:
            messagebox.showinfo(APP_NAME, "먼저 음악 폴더를 선택해 곡을 불러오세요.")
            return
        if self.tag_window and self.tag_window.winfo_exists():
            self.tag_window.focus()
            return
        sel = self.tree.selection()
        if len(sel) >= 2:
            targets, scope = [self.view_tracks[int(i)] for i in sel], "선택한 곡"
        else:
            targets = list(self.view_tracks)
            scope = "검색 결과" if self.search.get().strip() else self.view_title.cget("text")
        self.tag_window = TagFixWindow(self, [t["path"] for t in targets], scope)

    def open_editor(self, paths):
        tracks = [self.by_path[p] for p in paths if p in self.by_path]
        if tracks:
            EditWindow(self, tracks)

    def release_files(self, paths):
        """수정할 파일 중 재생 중인 곡이 있으면 잠시 멈춘다. 저장 후 이어서 재생할 정보를 돌려준다."""
        if self.current and self.current["path"] in set(paths) and self.player.loaded:
            resume = {"path": self.current["path"], "pos": self.player.position(), "paused": self.player.paused}
            self.player.release()
            self.update_play_icon()
            return resume
        return None

    def apply_file_changes(self, changes, resume=None):
        """태그 정리로 바뀐 파일만 다시 읽어 목록을 갱신한다(라이브러리 전체를 다시 스캔하지 않음).
        changes: [(이전 경로, 새 경로)]"""
        t0 = time.monotonic()
        mapping = {o: n for o, n in changes if o != n}
        if mapping:
            self.favorites = {mapping.get(p, p) for p in self.favorites}
            self.data["favorites"] = sorted(self.favorites)
            for name, paths in self.data["playlists"].items():
                self.data["playlists"][name] = [mapping.get(p, p) for p in paths]
            save_data(self.data)
        gone = {o for o, _ in changes}
        fresh = {n: read_track(n) for _, n in changes if os.path.exists(n)}
        self.tracks = [t for t in self.tracks if t["path"] not in gone and t["path"] not in fresh] + list(fresh.values())
        self.tracks.sort(key=lambda t: (t["artist"].casefold(), t["album"].casefold(), t["title"].casefold()))
        self.by_path = {t["path"]: t for t in self.tracks}
        if self.queue:
            cur = self.queue[self.queue_index]["path"] if 0 <= self.queue_index < len(self.queue) else None
            self.queue = [self.by_path[mapping.get(t["path"], t["path"])] for t in self.queue
                          if mapping.get(t["path"], t["path"]) in self.by_path]
            new_cur = mapping.get(cur, cur)
            self.queue_index = next((i for i, t in enumerate(self.queue) if t["path"] == new_cur), 0)
        if self.current:
            self.current = self.by_path.get(mapping.get(self.current["path"], self.current["path"]))
            if self.current:
                self.update_now_playing()
        update_scan_cache(fresh.values(), removed=gone - set(fresh))
        self.refresh_list()
        if resume:  # 잠시 멈췄던 곡을 새 경로에서 같은 위치부터 이어서 재생
            t = self.by_path.get(mapping.get(resume["path"], resume["path"]))
            if t:
                try:
                    self.player.play(t["path"], t["length"], start=resume["pos"])
                    if resume["paused"]:
                        self.player.pause()
                    self.current = t
                    self.update_now_playing()
                    self.mark_playing()
                except Exception:
                    pass
        ulog(f"list refresh after apply: {len(fresh)} files in {time.monotonic() - t0:.2f}s")

    def remap_paths(self, mapping):
        """파일 이름이 바뀐 뒤 즐겨찾기·플레이리스트·대기열 경로를 맞추고 다시 스캔한다."""
        mapping = {o: n for o, n in mapping.items() if o != n}
        if mapping:
            self.favorites = {mapping.get(p, p) for p in self.favorites}
            self.data["favorites"] = sorted(self.favorites)
            for name, paths in self.data["playlists"].items():
                self.data["playlists"][name] = [mapping.get(p, p) for p in paths]
            save_data(self.data)
            self._path_remap = mapping
        folder = self.data.get("music_folder")
        if folder and os.path.isdir(folder):
            self.start_scan(folder)

    # ───── 기타 ─────
    def toggle_theme(self):
        mode = "dark" if self.theme_switch.get() else "light"
        ctk.set_appearance_mode(mode)
        self.data["appearance"] = mode
        save_data(self.data)
        self.apply_tree_style()

    def on_close(self):
        save_data(self.data)
        try:
            pygame.mixer.quit()
        except pygame.error:
            pass
        self.destroy()


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--apply-update":
        try:
            run_update_helper(sys.argv[2], int(sys.argv[3]))
        except Exception as e:  # 설치 모드 오류는 기록만 남긴다
            ulog(f"helper error: {e!r}")
            ctypes_msg(f"업데이트 중 오류가 발생했습니다.\n{e}")
        sys.exit(0)
    load_bundled_fonts()
    if getattr(sys, "frozen", False):
        t = threading.Timer(15, cleanup_old_updates)  # 지난 방식의 업데이트 임시 폴더 정리
        t.daemon = True
        t.start()
    MusicApp().mainloop()


if __name__ == "__main__":
    main()
