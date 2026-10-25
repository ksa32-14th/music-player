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
from PIL import Image, ImageDraw

APP_NAME = "Music Player"
DATA_DIR = Path(os.getenv("APPDATA") or Path.home()) / "ModernMusicPlayer"
DATA_FILE = DATA_DIR / "data.json"
UI_FONT = "Malgun Gothic" if sys.platform.startswith("win") else "Helvetica"
ACCENT = "#1fb85a"
ACCENT_RGB = (31, 184, 90)
UNKNOWN_ARTIST = "알 수 없는 아티스트"
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
    """GitHub 최신 릴리스를 조회한다. 반환: (버전 문자열, zip 다운로드 주소) 또는 None."""
    with http_get(f"https://api.github.com/repos/{repo}/releases/latest") as r:
        data = json.load(r)
    asset = next((a for a in data.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
    if not asset:
        return None
    return data.get("tag_name", ""), asset["browser_download_url"]


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


UPDATER_PS1 = r"""param([int]$AppPid, [string]$Src, [string]$Dst, [string]$Exe)
Wait-Process -Id $AppPid -Timeout 60 -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
robocopy $Src $Dst /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -lt 8) { Remove-Item -LiteralPath (Split-Path $Src -Parent) -Recurse -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath $Exe
"""


def launch_updater(new_dir):
    """프로그램이 종료되기를 기다렸다가 새 파일을 복사하고 다시 실행하는 PowerShell을 띄운다."""
    install_dir = Path(sys.executable).resolve().parent
    script = Path(tempfile.gettempdir()) / "mmp_updater.ps1"
    script.write_text(UPDATER_PS1, encoding="utf-8-sig")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", str(script), "-AppPid", str(os.getpid()), "-Src", str(new_dir),
         "-Dst", str(install_dir), "-Exe", str(install_dir / EXE_NAME)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        close_fds=True,
    )


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
    title = artist = album = ""
    length = 0.0
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            length = float(getattr(audio.info, "length", 0) or 0)
            if audio.tags:
                title = _first(audio.tags, "title")
                artist = _first(audio.tags, "artist")
                album = _first(audio.tags, "album")
    except Exception:
        pass
    return {
        "path": str(p),
        "file": p.stem,
        "title": title or p.stem,
        "artist": artist or UNKNOWN_ARTIST,
        "album": album or UNKNOWN_ALBUM,
        "length": length,
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


def placeholder(size):
    """앨범 아트가 없을 때 쓰는 그라데이션 + 음표 이미지."""
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    top, bottom = (58, 58, 72), (22, 22, 28)
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    r = size * 0.11
    cx, cy = size * 0.44, size * 0.64
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT_RGB)
    stem_x = cx + r - size * 0.035
    d.rectangle((stem_x, size * 0.26, cx + r, cy), fill=ACCENT_RGB)
    d.polygon([(cx + r, size * 0.26), (cx + r + size * 0.16, size * 0.34), (cx + r, size * 0.40)], fill=ACCENT_RGB)
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


def ctk_icon(kind, size, light_color, dark_color):
    return ctk.CTkImage(
        light_image=make_icon(kind, size * 2, light_color),
        dark_image=make_icon(kind, size * 2, dark_color),
        size=(size, size),
    )


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


# ───────────────────────── 메인 앱 ─────────────────────────
class MusicApp(ctk.CTk):
    def __init__(self):
        self.data = load_data()
        ctk.set_appearance_mode(self.data.get("appearance", "dark"))
        ctk.set_default_color_theme("green")
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
        self.pl_buttons = {}

        self.ph_big = ctk.CTkImage(rounded(placeholder(504), 504, 36), size=(252, 252))
        self.ph_small = ctk.CTkImage(rounded(placeholder(112), 112, 14), size=(56, 56))
        self.ic_play = ctk_icon("play", 22, "#ffffff", "#ffffff")
        self.ic_pause = ctk_icon("pause", 22, "#ffffff", "#ffffff")
        self.ic_prev = ctk_icon("prev", 18, "#2b2b2b", "#e6e6e6")
        self.ic_next = ctk_icon("next", 18, "#2b2b2b", "#e6e6e6")
        self.ic_vol = ctk_icon("volume", 18, "#555555", "#b3b3b3")

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

        def progress(ratio):
            self.after(0, lambda: self.update_btn_state(f"업데이트를 내려받는 중… {int(ratio * 100)}%"))

        def worker():
            try:
                new_dir = download_and_extract(url, progress)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(APP_NAME, f"업데이트를 내려받지 못했습니다.\n{e}"))
                self.after(0, self.refresh_list)
                return
            self.after(0, lambda: self.finish_update(new_dir))

        threading.Thread(target=worker, daemon=True).start()

    def update_btn_state(self, text):
        self.view_count.configure(text=text)

    def finish_update(self, new_dir):
        try:
            launch_updater(new_dir)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"업데이트를 설치하지 못했습니다.\n{e}")
            return
        self.on_close()

    def font(self, size, weight="normal"):
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

        ctk.CTkLabel(sb, text="♪  Music", font=self.font(22, "bold"), anchor="w").grid(
            row=0, column=0, padx=22, pady=(26, 18), sticky="ew")
        self.nav_all = self._nav_button(sb, "모든 곡", lambda: self.set_view("all"))
        self.nav_all.grid(row=1, column=0, padx=12, pady=2, sticky="ew")
        self.nav_fav = self._nav_button(sb, "즐겨찾기", lambda: self.set_view("favorites"))
        self.nav_fav.grid(row=2, column=0, padx=12, pady=2, sticky="ew")

        head = ctk.CTkFrame(sb, fg_color="transparent")
        head.grid(row=3, column=0, padx=12, pady=(22, 4), sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="플레이리스트", font=self.font(12, "bold"),
                     text_color=("#6b6b6b", "#8a8a8a"), anchor="w").grid(row=0, column=0, padx=10, sticky="w")
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
                      command=self.choose_folder).grid(row=6, column=0, padx=18, pady=(2, 10), sticky="ew")

        self.theme_switch = ctk.CTkSwitch(sb, text="다크 모드", font=self.font(12), command=self.toggle_theme)
        self.theme_switch.grid(row=7, column=0, padx=22, pady=(0, 20), sticky="w")
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

        cols = ("fav", "title", "artist", "album", "time")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", style="Music.Treeview", selectmode="extended")
        for c, text in zip(cols, ("", "제목", "아티스트", "앨범", "시간")):
            self.tree.heading(c, text=text, anchor="e" if c == "time" else ("center" if c == "fav" else "w"))
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
        self.fav_btn = ctk.CTkButton(left, text="♡", width=36, height=36, corner_radius=18, font=self.font(20),
                                     fg_color="transparent", hover_color=("#dcdcdc", "#222222"),
                                     text_color=("#2b2b2b", "#e6e6e6"), command=self.toggle_current_fav)
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
        sel = "#26352c" if dark else "#dcf2e4"
        scale = ctk.ScalingTracker.get_window_scaling(self)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Music.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=int(36 * scale), borderwidth=0, font=(UI_FONT, 10))
        style.configure("Music.Treeview.Heading", background=bg, foreground=sub, borderwidth=0,
                        relief="flat", font=(UI_FONT, 9, "bold"), padding=(6, 8))
        style.map("Music.Treeview", background=[("selected", sel)], foreground=[("selected", fg)])
        style.map("Music.Treeview.Heading", background=[("active", bg)], foreground=[("active", fg)])
        style.layout("Music.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        widths = {"fav": 44, "title": 300, "artist": 200, "album": 200, "time": 64}
        for c, w in widths.items():
            self.tree.column(c, width=int(w * scale), minwidth=int(min(w, 60) * scale),
                             stretch=c not in ("fav", "time"),
                             anchor="e" if c == "time" else ("center" if c == "fav" else "w"))
        self.tree.tag_configure("playing", foreground=ACCENT)

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
            files = []
            for root, _, names in os.walk(folder):
                files += [os.path.join(root, n) for n in names if n.lower().endswith(".mp3")]
            state["total"] = len(files)
            result = []
            for i, f in enumerate(files, 1):
                result.append(read_track(f))
                state["count"] = i
            result.sort(key=lambda t: (t["artist"].casefold(), t["album"].casefold(), t["title"].casefold()))
            state["result"] = result
            state["done"] = True

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
                     or q in t["album"].casefold() or q in t["file"].casefold()]
        self.view_tracks = items
        self.tree.delete(*self.tree.get_children())
        cur = self.current["path"] if self.current else None
        for i, t in enumerate(items):
            self.tree.insert("", "end", iid=str(i), tags=("playing",) if t["path"] == cur else (),
                             values=("♥" if t["path"] in self.favorites else "♡",
                                     t["title"], t["artist"], t["album"], fmt_time(t["length"])))
        names = {"all": "모든 곡", "favorites": "즐겨찾기"}
        self.view_title.configure(text=names.get(self.view) if isinstance(self.view, str) else self.view[1])
        total = len(self.base_tracks())
        self.view_count.configure(text=f"검색 결과 {len(items)}곡 / 전체 {total}곡" if q else f"{total}곡")

    def mark_playing(self):
        cur = self.current["path"] if self.current else None
        for iid in self.tree.get_children():
            t = self.view_tracks[int(iid)]
            self.tree.item(iid, tags=("playing",) if t["path"] == cur else ())

    def update_fav_cells(self):
        for iid in self.tree.get_children():
            t = self.view_tracks[int(iid)]
            self.tree.set(iid, "fav", "♥" if t["path"] in self.favorites else "♡")

    # ───── 목록 이벤트 ─────
    def on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) != "#1":
            self.play_from_view(int(row))

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
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
        self.fav_btn.configure(text="♥" if on else "♡",
                               text_color=ACCENT if on else ("#2b2b2b", "#e6e6e6"))

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


if __name__ == "__main__":
    MusicApp().mainloop()
