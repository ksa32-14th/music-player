"""
Music Player 실행 파일의 시작점.

exe 안에 든 코드(빌드 시점 버전)와, 자동 업데이트로 내려받은 코드
(%APPDATA%\\ModernMusicPlayer\\code\\<버전>\\) 중 더 새 것을 불러와 실행한다.
내려받은 코드가 시작 중에 오류를 내면 그 버전을 '불량'으로 표시하고 exe 안의 코드로 실행한다.
"""
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path

LAUNCHER_VERSION = 1
DATA_DIR = Path(os.getenv("APPDATA") or Path.home()) / "ModernMusicPlayer"
CODE_DIR = DATA_DIR / "code"
BAD_FILE = CODE_DIR / "bad_versions.json"
MODULES = ("tag_fixer", "music_player")  # 의존 순서대로 불러온다


def log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [launcher] {msg}\n")
    except OSError:
        pass


def vt(v):
    out = []
    for piece in str(v).lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def bundled_version():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        with open(base / "app_info.json", encoding="utf-8-sig") as f:
            return str(json.load(f).get("version", "0"))
    except (OSError, ValueError):
        return "0"


def bad_versions():
    try:
        with open(BAD_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def mark_bad(version):
    bad = bad_versions() | {version}
    try:
        with open(BAD_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(bad), f)
    except OSError:
        pass


def candidates(min_version):
    if not CODE_DIR.is_dir():
        return []
    bad = bad_versions()
    found = []
    for d in CODE_DIR.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.name not in bad and vt(d.name) > vt(min_version) \
                and all((d / f"{m}.py").is_file() for m in MODULES):
            found.append(d)
    return sorted(found, key=lambda d: vt(d.name), reverse=True)


def load_from(folder):
    mods = {}
    for name in MODULES:
        spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod  # 다음 모듈의 import가 이것을 쓰도록 먼저 등록
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods["music_player"]


def unload():
    for name in MODULES:
        sys.modules.pop(name, None)


def run_bundled(version):
    import music_player  # exe에 들어 있는 코드 (PyInstaller가 이 import를 보고 필요한 라이브러리를 함께 넣는다)
    music_player.APP_VERSION = version
    music_player.LAUNCHER_VERSION = LAUNCHER_VERSION
    music_player.main()


def main():
    version = bundled_version()
    if len(sys.argv) > 1 and sys.argv[1] == "--apply-update":  # 예전 방식 업데이트 호환
        return run_bundled(version)

    for folder in candidates(version):
        started = time.monotonic()
        try:
            mod = load_from(folder)
            if getattr(mod, "REQUIRES_LAUNCHER", 1) > LAUNCHER_VERSION:
                log(f"code {folder.name} needs newer launcher; skip")
                unload()
                continue
            mod.APP_VERSION = folder.name
            mod.LAUNCHER_VERSION = LAUNCHER_VERSION
            log(f"start code {folder.name}")
        except Exception:
            log(f"code {folder.name} failed to load:\n{traceback.format_exc()}")
            mark_bad(folder.name)
            unload()
            continue
        try:
            mod.main()
            return
        except SystemExit:
            raise
        except Exception:
            log(f"code {folder.name} crashed:\n{traceback.format_exc()}")
            if time.monotonic() - started < 20:  # 시작 직후 오류면 불량 버전으로 보고 이전 코드로 실행
                mark_bad(folder.name)
                unload()
                break
            raise
    log(f"start bundled code {version}")
    run_bundled(version)


if __name__ == "__main__":
    main()
