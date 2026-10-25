"""
태그 정리: MusicBrainz에서 곡 정보를 찾아 제목·아티스트·앨범·장르·파일명·커버 수정안을 만들고 적용한다.
UI와 분리된 로직만 담는다.

- 곡 정보: MusicBrainz API (IP당 평균 초당 1회 제한, 연락처가 담긴 User-Agent 필수)
- 커버: Cover Art Archive
"""
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import APIC, ID3, TALB, TCON, TIT2, TPE1, ID3NoHeaderError

USER_AGENT = "MusicPlayer/1.0 ( https://github.com/ksa32-14th/music-player )"
MB_SEARCH = "https://musicbrainz.org/ws/2/recording"
CAA = "https://coverartarchive.org"
MB_INTERVAL = 1.1  # 초. MusicBrainz 제한(초당 1회)보다 약간 여유를 둔다.
FIELDS = ("title", "artist", "album", "genre")
FIELD_LABEL = {"title": "제목", "artist": "아티스트", "album": "앨범", "genre": "장르"}
TEXT_FRAMES = {"title": TIT2, "artist": TPE1, "album": TALB, "genre": TCON}
FRAME_IDS = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "genre": "TCON"}


# ───────────────────────── 문자열 도구 ─────────────────────────
def fix_text(value):
    """CP949(한국어) 태그가 Latin-1로 잘못 저장되어 깨진 경우 복구를 시도한다."""
    if not value:
        return ""
    s = str(value).strip()
    if any("\x80" <= ch <= "\xff" for ch in s) and all(ord(ch) <= 0xFF for ch in s):
        try:
            return s.encode("latin-1").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def has_hangul(s):
    return any("가" <= ch <= "힣" or "ㄱ" <= ch <= "ㆎ" for ch in s or "")


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(ch for ch in s if ch.isalnum())


def norm_core(s):
    """괄호 안 내용(feat., Remastered 등)을 뺀 비교용 문자열."""
    return norm(re.sub(r"[\(\[\{].*?[\)\]\}]", "", s or ""))


def similarity(a, b):
    a1, b1 = norm(a), norm(b)
    if not a1 or not b1:
        return 0.0
    if a1 == b1:
        return 1.0
    return max(SequenceMatcher(None, a1, b1).ratio(),
               SequenceMatcher(None, norm_core(a), norm_core(b)).ratio() if norm_core(a) and norm_core(b) else 0)


def safe_filename(name, limit=150):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:limit].rstrip(". ") or "untitled"


def guess_from_filename(stem):
    """'01. 아티스트 - 제목' 같은 파일명에서 (아티스트, 제목)을 추정한다."""
    s = re.sub(r"[_]+", " ", stem)
    s = re.sub(r"^\s*\d{1,3}\s*[\.\-)]\s*", "", s)  # 앞쪽 트랙 번호 ('01. ', '01 - ')
    if re.match(r"^\s*\d{1,3}\s+\S", s) and re.search(r"\s[-–]\s", s):  # '01 아티스트 - 제목'
        s = re.sub(r"^\s*\d{1,3}\s+", "", s)
    s = re.sub(r"\s*[\(\[](mp3|320k|128k|official.*?|lyrics?|audio|mv|m/v)[\)\]]\s*", " ", s, flags=re.I)
    parts = re.split(r"\s+-\s+|\s+–\s+", s, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", s.strip()


def pretty_genre(tag):
    special = {"k-pop": "K-Pop", "j-pop": "J-Pop", "c-pop": "C-Pop", "r&b": "R&B", "edm": "EDM",
               "hip hop": "Hip Hop", "k-hip hop": "K-Hip Hop", "ost": "OST", "k-ballad": "K-Ballad"}
    t = tag.strip().lower()
    return special.get(t) or " ".join(w[:1].upper() + w[1:] for w in t.split())


# ───────────────────────── 파일 읽기 ─────────────────────────
def read_raw(path):
    """파일의 실제 태그 값을 읽는다. 없으면 None."""
    raw = {f: None for f in FIELDS}
    raw["has_cover"] = False
    raw["length"] = 0.0
    try:
        audio = MutagenFile(path)
        raw["length"] = float(getattr(audio.info, "length", 0) or 0) if audio else 0.0
    except Exception:
        pass
    try:
        tags = ID3(path)
    except Exception:
        return raw
    for f, fid in FRAME_IDS.items():
        frame = tags.get(fid)
        if frame and frame.text:
            raw[f] = str(frame.text[0]).strip() or None
    raw["has_cover"] = bool(tags.getall("APIC"))
    return raw


# ───────────────────────── 네트워크 ─────────────────────────
class Http:
    """MusicBrainz 요청 간격을 지키는 HTTP 도우미. 테스트에서는 fetch를 바꿔 끼운다."""

    def __init__(self, fetch=None):
        self._last_mb = 0.0
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def mb_json(self, url):
        for attempt in range(3):
            wait = MB_INTERVAL - (time.monotonic() - self._last_mb)
            if wait > 0:
                time.sleep(wait)
            self._last_mb = time.monotonic()
            try:
                return json.loads(self.fetch(url))
            except urllib.error.HTTPError as e:
                if e.code == 503 and attempt < 2:  # 요청 제한: 잠시 쉬고 다시
                    time.sleep(3)
                    continue
                raise
        return {}

    def get_bytes(self, url):
        try:
            return self.fetch(url, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise


def lucene_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_recordings(http, title, artist):
    q = f"recording:{lucene_quote(title)}"
    if artist:
        q += f" AND artist:{lucene_quote(artist)}"
    url = f"{MB_SEARCH}?{urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': 10})}"
    recs = http.mb_json(url).get("recordings", [])
    if not recs and artist:  # 정확 검색 실패 시 느슨하게 한 번 더
        url = f"{MB_SEARCH}?{urllib.parse.urlencode({'query': f'{title} {artist}', 'fmt': 'json', 'limit': 10})}"
        recs = http.mb_json(url).get("recordings", [])
    return recs


def credit_name(rec):
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in rec.get("artist-credit", [])).strip()


def pick_release(rec):
    """정규 발매(Official) 중 앨범 > EP > 싱글 순, 같은 종류면 발매일이 빠른 것을 고른다."""
    rank = {"Album": 0, "EP": 1, "Single": 2}

    def key(r):
        rg = r.get("release-group", {})
        secondary = rg.get("secondary-types") or []
        return (r.get("status") != "Official", bool(secondary),
                rank.get(rg.get("primary-type"), 3), r.get("date") or "9999")

    rels = rec.get("releases") or []
    return min(rels, key=key) if rels else None


def best_match(recs, title, artist, length):
    best, best_score = None, 0.0
    for rec in recs:
        t_sim = similarity(title, rec.get("title", ""))
        credit = credit_name(rec)
        rec_len = (rec.get("length") or 0) / 1000
        diff = abs(length - rec_len) if length and rec_len else None
        if artist and has_hangul(artist) != has_hangul(credit):
            # '아이유'와 'IU'처럼 표기 문자가 다르면 이름을 비교할 수 없으므로
            # 제목이 거의 같고 곡 길이까지 맞을 때만 인정한다
            if t_sim < 0.9 or diff is None or diff > 5:
                continue
            a_sim = 0.7
        else:
            a_sim = similarity(artist, credit) if artist else 0.7
            if t_sim < 0.6 or (artist and a_sim < 0.45):
                continue
        score = 0.55 * t_sim + 0.30 * a_sim + 0.15 * (rec.get("score", 0) / 100)
        if diff is not None:
            score += 0.05 if diff <= 3 else (0 if diff <= 10 else -0.15)
        if score > best_score:
            best, best_score = rec, score
    return (best, round(min(best_score, 1.0), 2)) if best_score >= 0.7 else (None, 0.0)


def fetch_cover(http, release):
    """커버를 내려받아 (bytes, mime)으로 돌려준다. 앨범 그룹 → 개별 발매 순으로 찾는다."""
    if not release:
        return None
    urls = []
    rgid = release.get("release-group", {}).get("id")
    if rgid:
        urls.append(f"{CAA}/release-group/{rgid}/front-500")
    urls.append(f"{CAA}/release/{release['id']}/front-500")
    for url in urls:
        try:
            data = http.get_bytes(url)
        except Exception:
            continue
        if data:
            try:
                from PIL import Image
                fmt = Image.open(BytesIO(data)).format
            except Exception:
                continue
            if fmt in ("JPEG", "PNG"):
                return data, "image/jpeg" if fmt == "JPEG" else "image/png"
    return None


# ───────────────────────── 수정안 만들기 ─────────────────────────
def decide(current, candidate):
    """현재 값과 후보 값을 비교해 바꿀 값을 정한다. 바꿀 필요가 없으면 None."""
    cur = current or ""
    fixed = fix_text(cur)
    if candidate:
        if not cur:
            return candidate
        if fixed != cur:  # 깨진 글자
            return candidate if similarity(fixed, candidate) >= 0.6 else fixed
        if cur == candidate:
            return None
        # 한글로 된 이름을 영문 표기로 바꾸지 않는다
        if has_hangul(cur) and not has_hangul(candidate):
            return None
        # 대소문자·띄어쓰기·오타 수준의 차이만 고친다
        if similarity(cur, candidate) >= 0.75:
            return candidate
        return None
    return fixed if fixed != cur else None


def analyze(path, http, want_cover=True):
    """한 곡을 분석해 수정안을 돌려준다."""
    p = Path(path)
    raw = read_raw(path)
    cur = {f: fix_text(raw[f]) if raw[f] else "" for f in FIELDS}
    g_artist, g_title = guess_from_filename(p.stem)
    q_title = cur["title"] or g_title
    q_artist = cur["artist"] or g_artist

    match, confidence, release, cand = None, 0.0, None, {}
    error = None
    if q_title:
        try:
            match, confidence = best_match(search_recordings(http, q_title, q_artist), q_title, q_artist,
                                           raw["length"])
        except Exception as e:
            error = str(e)
    if match:
        release = pick_release(match)
        tags = sorted(match.get("tags") or [], key=lambda t: -t.get("count", 0))
        cand = {
            "title": match.get("title", ""),
            "artist": credit_name(match),
            "album": release.get("title", "") if release else "",
            "genre": pretty_genre(tags[0]["name"]) if tags else "",
        }

    if not match:  # 온라인에서 못 찾았고 태그가 비어 있으면 파일명에서 추정한 값을 쓴다
        cand = {"title": g_title if not raw["title"] else "", "artist": g_artist if not raw["artist"] else ""}

    changes = {}
    for f in FIELDS:
        new = decide(raw[f], cand.get(f, ""))
        if f == "genre" and raw[f]:  # 이미 있는 장르는 건드리지 않는다
            new = None if fix_text(raw[f]) == raw[f] else fix_text(raw[f])
        if new and new != (raw[f] or ""):
            changes[f] = (raw[f] or "", new)

    cover = None
    if want_cover and match and not raw["has_cover"]:
        cover = fetch_cover(http, release)

    final_title = changes.get("title", (None, cur["title"]))[1] or q_title
    final_artist = changes.get("artist", (None, cur["artist"]))[1] or q_artist
    new_name = None
    if final_title and final_artist:
        candidate = safe_filename(f"{final_artist} - {final_title}") + p.suffix.lower()
        if candidate != p.name:
            new_name = candidate

    return {
        "path": str(p),
        "changes": changes,
        "cover": cover,
        "new_name": new_name,
        "confidence": confidence,
        "matched": bool(match),
        "error": error,
    }


def has_anything(sug):
    return bool(sug["changes"] or sug["cover"] or sug["new_name"])


def summarize(sug, use_name=True, use_cover=True):
    parts = []
    for f, (old, new) in sug["changes"].items():
        parts.append(f"{FIELD_LABEL[f]}: {old or '(없음)'} → {new}")
    if use_cover and sug["cover"]:
        parts.append("커버 추가")
    if use_name and sug["new_name"]:
        parts.append(f"파일명 → {sug['new_name']}")
    return " · ".join(parts)


# ───────────────────────── 적용·되돌리기 ─────────────────────────
def _unique_path(target, original):
    """같은 이름의 다른 파일이 있으면 ' (2)'처럼 번호를 붙인다."""
    if not target.exists() or _same_file(target, original):
        return target
    for i in range(2, 1000):
        cand = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(str(target))


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def apply(sug, use_name=True, use_cover=True, use_fields=FIELDS):
    """수정안을 파일에 쓴다. 되돌리기용 기록을 돌려준다."""
    path = Path(sug["path"])
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    backup = {"old_path": str(path), "new_path": str(path), "fields": {}, "cover_added": False}
    changed = False
    for f, (_, new) in sug["changes"].items():
        if f not in use_fields:
            continue
        old_frame = tags.get(FRAME_IDS[f])
        backup["fields"][f] = str(old_frame.text[0]) if old_frame and old_frame.text else None
        tags.setall(FRAME_IDS[f], [TEXT_FRAMES[f](encoding=1, text=[new])])
        changed = True
    if use_cover and sug["cover"] and not tags.getall("APIC"):
        data, mime = sug["cover"]
        tags.add(APIC(encoding=1, mime=mime, type=3, desc="Cover", data=data))
        backup["cover_added"] = True
        changed = True
    if changed:
        tags.save(path, v2_version=3)  # Windows 탐색기 호환을 위해 ID3v2.3으로 저장

    if use_name and sug["new_name"]:
        target = _unique_path(path.with_name(sug["new_name"]), path)
        if target != path:
            os.rename(path, target)
            backup["new_path"] = str(target)
    return backup


def undo(entry):
    """apply()가 남긴 기록으로 원래대로 되돌린다. 되돌린 뒤의 경로를 돌려준다."""
    path = Path(entry["new_path"])
    if entry["fields"] or entry["cover_added"]:
        tags = ID3(path)
        for f, old in entry["fields"].items():
            if old is None:
                tags.delall(FRAME_IDS[f])
            else:
                tags.setall(FRAME_IDS[f], [TEXT_FRAMES[f](encoding=1, text=[old])])
        if entry["cover_added"]:
            tags.delall("APIC")
        tags.save(path, v2_version=3)
    old = Path(entry["old_path"])
    if old != path:
        if old.exists() and not _same_file(old, path):
            raise FileExistsError(f"원래 이름의 파일이 이미 있습니다: {old.name}")
        os.rename(path, old)
    return str(old)
