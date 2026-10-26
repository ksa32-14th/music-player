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
DZ = "https://api.deezer.com"
DZ_INTERVAL = 0.12  # 초. 흔히 알려진 Deezer 제한(5초에 50회)보다 낮은 초당 약 8회. 초과 오류가 오면 쉬었다가 다시 보낸다.
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


NAME_FORMATS = {"title_artist": "제목_아티스트", "artist - title": "아티스트 - 제목"}
_FS_REPLACE = {":": "-", "/": "-", "\\": "-", "|": "-", "?": "", "*": "", '"': "'", "<": "(", ">": ")"}


def safe_filename(name, limit=150):
    name = "".join(_FS_REPLACE.get(ch, ch) for ch in name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:limit].rstrip(". ") or "untitled"


def build_filename(title, artist, fmt, suffix=".mp3"):
    t, a = safe_filename(title), safe_filename(artist)
    stem = f"{t}_{a}" if fmt == "title_artist" else f"{a} - {t}"
    return stem + suffix


def parse_filename(stem, fmt="title_artist"):
    """파일명에서 (아티스트, 제목)을 추정한다. fmt는 사용자의 파일명 형식."""
    s = re.sub(r"^\s*\d{1,3}\s*[\.\-)]\s*", "", stem)  # 앞쪽 트랙 번호 ('01. ', '01 - ')
    s = re.sub(r"\s*[\(\[](mp3|320k|128k|official.*?|lyrics?|audio|mv|m/v)[\)\]]\s*", " ", s, flags=re.I).strip()
    if fmt == "title_artist":
        if "_" in s:
            title, artist = s.rsplit("_", 1)
            return artist.strip(), title.strip()
        return "", s
    parts = re.split(r"\s+-\s+|\s+–\s+", s, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", s


def guess_from_filename(stem, fmt="artist - title"):
    return parse_filename(stem, fmt)


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
        self._last_dz = 0.0
        self._dz_lock = __import__("threading").Lock()
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

    def dz_json(self, url):
        """Deezer 요청. 초당 5회 이하로 보내고, 할당량 초과 오류(code 4)면 잠시 쉬고 다시 시도한다."""
        for attempt in range(4):
            with self._dz_lock:
                wait = DZ_INTERVAL - (time.monotonic() - self._last_dz)
                if wait > 0:
                    time.sleep(wait)
                self._last_dz = time.monotonic()
            data = json.loads(self.fetch(url))
            err = data.get("error") if isinstance(data, dict) else None
            if err and err.get("code") == 4 and attempt < 3:
                time.sleep(5)
                continue
            if err:
                raise RuntimeError(f"Deezer: {err.get('message', err)}")
            return data
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
    for r in recs:
        r["_artist_matched"] = bool(artist)  # MusicBrainz가 아티스트 조건(별칭 포함)으로 찾은 결과
    return recs


MB_RG_SEARCH = "https://musicbrainz.org/ws/2/release-group"


def search_release_groups(http, album, artist):
    q = f"releasegroup:{lucene_quote(album)}"
    if artist:
        q += f" AND artist:{lucene_quote(artist)}"
    url = f"{MB_RG_SEARCH}?{urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': 10})}"
    return http.mb_json(url).get("release-groups", [])


def best_release_group(rgs, album):
    """아티스트 조건으로 찾은 결과 중 앨범 제목이 가장 잘 맞는 것. 엉뚱한 커버를 막기 위해 기준을 높게 둔다."""
    best, best_score = None, 0.0
    for rg in rgs:
        sim = similarity(album, rg.get("title", ""))
        score = 0.8 * sim + 0.2 * (rg.get("score", 0) / 100)
        if sim >= 0.85 and score > best_score:
            best, best_score = rg, score
    return (best, round(min(best_score, 1.0), 2)) if best else (None, 0.0)


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
            # '아이유'와 'IU'처럼 표기 문자가 다르면 이름을 비교할 수 없다. MusicBrainz가 아티스트 조건으로
            # 찾았고 제목이 거의 같으면 인정하고, 그렇지 않으면 곡 길이까지 맞을 때만 인정한다
            if t_sim < 0.9 or (not rec.get("_artist_matched") and (diff is None or diff > 5)):
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


def fetch_rg_cover(http, rgid, release_ids=()):
    """앨범(release-group) 대표 커버 → 개별 발매 커버 순으로 찾는다. Cover Art Archive는 요청 제한이 없다."""
    urls = [f"{CAA}/release-group/{rgid}/front-500"] + [f"{CAA}/release/{r}/front-500" for r in release_ids[:2]]
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


# ───────────────────────── 표기 통일(라이브러리 내부) ─────────────────────────
_NOT_ALIAS = re.compile(r"^(feat|ft|with|prod|remix|ver|version|inst|live|ost|mr|from|cover|original|"
                        r"acoustic|explicit|clean|remaster|radio|edit|mix|bonus|demo)\b", re.I)


def name_keys(name):
    """같은 대상을 가리키는지 판단할 비교 키들. '아이유(IU)' → {'아이유iu', '아이유', 'iu'}
    괄호가 이름 끝에 있을 때만 별칭으로 본다('(여자)아이들'처럼 앞에 있는 괄호는 이름의 일부)."""
    keys = {norm(name)}
    m = re.match(r"^(.*?\S)\s*[\(\[]([^\)\]]+)[\)\]]\s*$", name)
    if m and not _NOT_ALIAS.match(m.group(2).strip()):
        for part in (m.group(1), m.group(2)):
            if len(norm(part)) >= 2:
                keys.add(norm(part))
    return {k for k in keys if k and not k.isdigit()}


def unify_names(counts):
    """{표기: 곡 수}를 받아 같은 대상끼리 묶고, 가장 많이 쓴 표기로 바꾸는 표를 돌려준다.
    반환: {표기: (대표 표기, {묶인 표기: 곡 수})} — 대표 표기와 다른 것만."""
    names = [n for n in counts if n]
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner = {}
    for n in names:
        for k in name_keys(n):
            if k in owner:
                parent[find(n)] = find(owner[k])
            else:
                owner[k] = n
    groups = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    result = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canon = max(members, key=lambda m: (counts[m], len(m)))
        detail = {m: counts[m] for m in sorted(members, key=lambda m: -counts[m])}
        for m in members:
            if m != canon:
                result[m] = (canon, detail)
    return result


def analyze_local(paths, library_paths=None, fmt="title_artist", progress=None, info=None):
    """인터넷 없이 라이브러리 안에서 표기 차이·깨진 글자·빈 태그·파일명을 점검한다."""
    library_paths = list(dict.fromkeys(list(library_paths or []) + list(paths)))
    raws = {}
    for i, p in enumerate(library_paths, 1):
        raws[p] = read_raw(p)
        if progress:
            progress(i, len(library_paths))

    if info is not None:  # 라이브러리에서 이미 쓰고 있는 장르(온라인 장르는 이 안에서만 고른다)
        genres = {}
        for p in library_paths:
            g = fix_text(raws[p]["genre"]) if raws[p]["genre"] else ""
            if g:
                genres[g] = genres.get(g, 0) + 1
        info["genres"] = genres

    def base_artist(p):
        r = raws[p]
        return fix_text(r["artist"]) if r["artist"] else parse_filename(Path(p).stem, fmt)[0]

    artist_counts = {}
    for p in library_paths:
        a = base_artist(p)
        if a:
            artist_counts[a] = artist_counts.get(a, 0) + 1
    artist_map = unify_names(artist_counts)

    def canon_artist(p):
        a = base_artist(p)
        return artist_map[a][0] if a in artist_map else a

    # 앨범은 같은 아티스트 안에서 대소문자·띄어쓰기·기호 차이만 맞춘다
    album_groups = {}
    for p in library_paths:
        al = fix_text(raws[p]["album"]) if raws[p]["album"] else ""
        if al:
            key = (norm(canon_artist(p)), norm(al))
            album_groups.setdefault(key, {}).setdefault(al, 0)
            album_groups[key][al] += 1
    album_map = {}
    for key, variants in album_groups.items():
        if len(variants) > 1:
            canon = max(variants, key=lambda v: (variants[v], len(v)))
            for v in variants:
                if v != canon:
                    album_map[(key[0], v)] = (canon, dict(variants))

    out = []
    for p in paths:
        r = raws[p]
        f_artist, f_title = parse_filename(Path(p).stem, fmt)
        changes, notes = {}, []

        title = fix_text(r["title"]) if r["title"] else f_title
        if title and title != (r["title"] or ""):
            changes["title"] = (r["title"] or "", title)
            notes.append("제목이 비어 있어 파일명에서 가져옴" if not r["title"] else "깨진 글자 복구")

        artist = canon_artist(p)
        if artist and artist != (r["artist"] or ""):
            changes["artist"] = (r["artist"] or "", artist)
            src = base_artist(p)
            if src in artist_map:
                detail = ", ".join(f"'{k}' {v}곡" for k, v in artist_map[src][1].items())
                notes.append(f"같은 아티스트의 다른 표기: {detail}")
            elif not r["artist"]:
                notes.append("아티스트가 비어 있어 파일명에서 가져옴")
            else:
                notes.append("깨진 글자 복구")

        if r["album"]:
            al = fix_text(r["album"])
            key = (norm(artist), al)
            if key in album_map:
                canon, detail = album_map[key]
                changes["album"] = (r["album"], canon)
                notes.append("같은 앨범의 다른 표기: " + ", ".join(f"'{k}' {v}곡" for k, v in detail.items()))
            elif al != r["album"]:
                changes["album"] = (r["album"], al)
        if r["genre"] and fix_text(r["genre"]) != r["genre"]:
            changes["genre"] = (r["genre"], fix_text(r["genre"]))

        out.append({
            "path": p, "changes": changes, "cover": None, "notes": notes,
            "final_title": title, "final_artist": artist,
            "missing": [k for k, v in (("album", r["album"]), ("genre", r["genre"])) if not v]
                       + ([] if r["has_cover"] else ["cover"]),
            "length": r["length"], "matched": False, "confidence": 0.0, "error": None,
            "raw_album": fix_text(r["album"]) if r["album"] else "",
        })
    return out


def filename_for(sug, fmt):
    if not (sug["final_title"] and sug["final_artist"]):
        return None
    name = build_filename(sug["final_title"], sug["final_artist"], fmt, Path(sug["path"]).suffix.lower())
    return name if name != Path(sug["path"]).name else None


# ───────────────────────── 온라인: Deezer + MusicBrainz 교차 확인 ─────────────────────────
GENRE_SYNONYMS = {
    "kpop": {"kpop", "koreanpop", "케이팝", "가요", "아이돌"},
    "hiphop": {"hiphop", "rap", "랩", "힙합", "rap/hiphop", "랩힙합", "khiphop"},
    "rnb": {"rnb", "r&b", "알앤비", "rnbsoul", "soul", "알앤비소울"},
    "ballad": {"ballad", "발라드", "kballad"},
    "dance": {"dance", "댄스", "electronic", "edm", "일렉트로닉"},
    "rock": {"rock", "록", "락", "metal", "록메탈"},
    "pop": {"pop", "팝"},
    "ost": {"ost", "soundtrack", "films/games", "filmsgames", "영화", "드라마"},
    "jpop": {"jpop", "jpop", "japanesepop", "제이팝"},
    "indie": {"indie", "인디", "alternative", "얼터너티브"},
    "jazz": {"jazz", "재즈"},
    "classical": {"classical", "클래식"},
}


def _gkey(g):
    return norm(g.replace("&", "n"))


def map_genre(candidates, library_genres):
    """온라인 장르 후보를 라이브러리에 이미 있는 장르로만 바꾼다. 맞는 것이 없으면 None.
    'Rap/Hip Hop'처럼 묶인 이름은 나눠서 비교하고, 같은 뜻의 표기(힙합/Hip-Hop 등)도 같은 것으로 본다."""
    if not library_genres:
        return None
    by_key = {}
    for g, n in library_genres.items():
        k = _gkey(g)
        if k and (k not in by_key or n > library_genres[by_key[k]]):
            by_key[k] = g

    def family(k):
        return next((f for f, keys in GENRE_SYNONYMS.items() if k in {_gkey(x) for x in keys} or k == f), None)

    for cand in candidates:
        pieces = [cand] + re.split(r"[/,&]| and ", cand)
        for piece in pieces:
            k = _gkey(piece)
            if not k:
                continue
            if k in by_key:
                return by_key[k]
            fam = family(k)
            if fam:
                same = [g for gk, g in by_key.items() if family(gk) == fam]
                if same:
                    return max(same, key=lambda g: library_genres[g])
    return None


def dz_quote(s):
    return '"' + s.replace('"', " ") + '"'


_DZ_GENRES = {}


_DZ_GENRE_LOCK = __import__("threading").Lock()


def dz_genre_names(http, album_id=None, genre_id=None):
    names = []
    if genre_id and genre_id > 0:
        with _DZ_GENRE_LOCK:
            if not _DZ_GENRES:
                for g in http.dz_json(f"{DZ}/genre").get("data", []):
                    _DZ_GENRES[g.get("id")] = g.get("name", "")
        if _DZ_GENRES.get(genre_id):
            return [_DZ_GENRES[genre_id]]
    if album_id:
        data = http.dz_json(f"{DZ}/album/{album_id}")
        names = [g.get("name", "") for g in (data.get("genres") or {}).get("data", [])]
        genre_id = genre_id or data.get("genre_id")
    if not names and genre_id and genre_id > 0:
        if not _DZ_GENRES:
            for g in http.dz_json(f"{DZ}/genre").get("data", []):
                _DZ_GENRES[g.get("id")] = g.get("name", "")
        if _DZ_GENRES.get(genre_id):
            names = [_DZ_GENRES[genre_id]]
    return [n for n in names if n]


def dz_lookup_album(http, artist, album):
    q = f"artist:{dz_quote(artist)} album:{dz_quote(album)}"
    items = http.dz_json(f"{DZ}/search/album?{urllib.parse.urlencode({'q': q, 'limit': 10})}").get("data", [])
    constrained = bool(items)  # Deezer가 아티스트 조건(별칭 포함)으로 찾은 결과
    if not items:  # 한글/영문 표기 차이 등으로 못 찾으면 자유 검색
        items = http.dz_json(f"{DZ}/search/album?{urllib.parse.urlencode({'q': f'{artist} {album}', 'limit': 10})}").get("data", [])
    best, best_s = None, 0.0
    for it in items:
        a_sim = similarity(album, it.get("title", ""))
        art = (it.get("artist") or {}).get("name", "")
        if has_hangul(artist) == has_hangul(art):
            r_sim = similarity(artist, art)
        else:  # '아이유'와 'IU'처럼 표기 문자가 다름: 아티스트 조건 검색으로 나온 경우만 같은 아티스트로 인정
            r_sim = 0.95 if constrained else 0.6
        if a_sim < 0.9 or r_sim < 0.5:
            continue
        sc = 0.7 * a_sim + 0.3 * r_sim
        if sc > best_s:
            best, best_s = it, sc
    if not best:
        return None
    return {"source": "Deezer", "album": best.get("title", ""), "score": round(best_s, 2), "kind": "album",
            "cover_url": best.get("cover_big") or best.get("cover_medium"),
            "album_id": best.get("id"), "genre_id": best.get("genre_id")}


def dz_lookup_track(http, title, artist, length):
    q = f"artist:{dz_quote(artist)} track:{dz_quote(title)}" if artist else f"track:{dz_quote(title)}"
    items = http.dz_json(f"{DZ}/search?{urllib.parse.urlencode({'q': q, 'limit': 10})}").get("data", [])
    if not items and artist:
        items = http.dz_json(f"{DZ}/search?{urllib.parse.urlencode({'q': f'{artist} {title}', 'limit': 10})}").get("data", [])
    best, best_s = None, 0.0
    for it in items:
        t_sim = similarity(title, it.get("title", ""))
        art = (it.get("artist") or {}).get("name", "")
        cross = bool(artist) and has_hangul(artist) != has_hangul(art)
        a_sim = 0.6 if cross or not artist else similarity(artist, art)
        dur = it.get("duration") or 0
        diff = abs(length - dur) if length and dur else None
        if t_sim < 0.85 or (not cross and artist and a_sim < 0.5):
            continue
        if diff is not None and diff > 4:  # 길이가 다르면 다른 버전(라이브·리믹스 등)일 가능성이 크다
            continue
        if cross and diff is None:
            continue
        sc = 0.55 * t_sim + 0.3 * a_sim + (0.15 if diff is not None and diff <= 2 else 0.05)
        if sc > best_s:
            best, best_s = it, sc
    if not best:
        return None
    al = best.get("album") or {}
    return {"source": "Deezer", "album": al.get("title", ""), "score": round(min(best_s, 1.0), 2), "kind": "track",
            "cover_url": al.get("cover_big") or al.get("cover_medium"), "album_id": al.get("id"), "genre_id": None}


def mb_lookup_album(http, artist, album):
    rg, conf = best_release_group(search_release_groups(http, album, artist), album)
    if not rg:
        return None
    tags = sorted(rg.get("tags") or [], key=lambda t: -t.get("count", 0))
    rel = [r["id"] for r in rg.get("releases", [])][:2]
    return {"source": "MusicBrainz", "album": rg.get("title", ""), "score": conf, "kind": "album",
            "rgid": rg["id"], "releases": rel, "genres": [t["name"] for t in tags[:3]]}


def mb_lookup_track(http, title, artist, length):
    match, conf = best_match(search_recordings(http, title, artist), title, artist, length)
    if not match:
        return None
    release = pick_release(match)
    if not release:
        return None
    tags = sorted(match.get("tags") or [], key=lambda t: -t.get("count", 0))
    return {"source": "MusicBrainz", "album": release.get("title", ""), "score": conf, "kind": "track",
            "rgid": (release.get("release-group") or {}).get("id"), "releases": [release["id"]],
            "genres": [t["name"] for t in tags[:3]]}


def _load_image(data):
    from PIL import Image
    try:
        img = Image.open(BytesIO(data))
        fmt = img.format
        img.load()
        return img, fmt
    except Exception:
        return None, None


def dhash(img):
    """이미지 지문(64비트). 두 커버가 같은 그림인지 비교하는 데 쓴다."""
    g = img.convert("L").resize((9, 8))
    px = list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def same_picture(h1, h2, limit=12):
    return bin(h1 ^ h2).count("1") <= limit


def download_cover(http, cand):
    urls = []
    if cand.get("cover_url"):
        urls.append(cand["cover_url"])
    if cand.get("rgid"):
        urls.append(f"{CAA}/release-group/{cand['rgid']}/front-500")
    urls += [f"{CAA}/release/{r}/front-500" for r in cand.get("releases", [])]
    for url in urls:
        try:
            data = http.get_bytes(url)
        except Exception:
            continue
        if not data:
            continue
        img, fmt = _load_image(data)
        if img is None or fmt not in ("JPEG", "PNG") or min(img.size) < 200:
            continue
        return {"data": data, "mime": "image/jpeg" if fmt == "JPEG" else "image/png",
                "source": cand["source"], "hash": dhash(img), "size": img.size}
    return None


def decide_group(g, library_genres):
    """한 그룹(앨범 또는 곡)의 Deezer·MusicBrainz 결과를 합쳐 앨범·장르·커버 제안과 신뢰도를 정한다."""
    cands = [c for c in (g.get("dz"), g.get("mb")) if c]
    out = {"album": None, "genre": None, "covers": [], "cover_level": None, "sources": [c["source"] for c in cands]}
    if not cands:
        return out
    # 앨범명(빈 경우에만 사용): 두 출처가 같으면 확실, 다르면 점수 높은 쪽
    if len(cands) == 2 and similarity(cands[0]["album"], cands[1]["album"]) >= 0.9:
        out["album"] = cands[0]["album"]
    else:
        out["album"] = max(cands, key=lambda c: c["score"])["album"]
    # 장르: 라이브러리에 있는 장르로만
    genre_cands = list(g.get("dz_genres") or []) + list((g.get("mb") or {}).get("genres") or [])
    out["genre"] = map_genre(genre_cands, library_genres)
    # 커버: 두 출처 이미지가 같은 그림이면 '높음', 하나뿐이면 앨범 단위로 정확히 찾았을 때만 '높음'
    covers = [c for c in (g.get("dz_cover"), g.get("mb_cover")) if c]
    if len(covers) == 2:
        if same_picture(covers[0]["hash"], covers[1]["hash"]):
            big = max(covers, key=lambda c: c["size"][0])
            out["covers"], out["cover_level"] = [big], "높음"
        else:
            covers.sort(key=lambda c: -next(x["score"] for x in cands if x["source"] == c["source"]))
            out["covers"], out["cover_level"] = covers, "출처 불일치"
    elif covers:
        src = next(x for x in cands if x["source"] == covers[0]["source"])
        strong = src["kind"] == "album" and src["score"] >= 0.95
        out["covers"], out["cover_level"] = covers, ("높음" if strong else "확인 필요")
    return out


def group_sugs(sugs):
    groups = {}
    for s in sugs:
        if not s["missing"] or not s["final_title"]:
            continue
        album = s["changes"].get("album", (None, None))[1] or s.get("raw_album") or ""
        key = (f"album|{norm(s['final_artist'])}|{norm(album)}" if album
               else f"track|{norm(s['final_artist'])}|{norm(s['final_title'])}")
        g = groups.setdefault(key, {"key": key, "album": album, "members": []})
        g["members"].append(s)
    # 커버가 없는 곡이 있는 그룹부터
    return sorted(groups.values(), key=lambda g: not any("cover" in m["missing"] for m in g["members"]))


def apply_group(g, library_genres):
    d = decide_group(g, library_genres)
    for m in g["members"]:
        m["online"] = d
        m["matched"] = bool(d["sources"])
        if "album" in m["missing"] and d["album"]:
            m["changes"]["album"] = ("", d["album"])
        if "genre" in m["missing"]:
            if d["genre"]:
                m["changes"]["genre"] = ("", d["genre"])
            else:
                m["changes"].pop("genre", None)
        if "cover" in m["missing"] and d["covers"]:
            m["cover_options"] = d["covers"]
            m["cover_level"] = d["cover_level"]
            idx = min(m.get("cover_choice", 0), len(d["covers"]) - 1)
            m["cover_choice"] = idx
            m["cover"] = (d["covers"][idx]["data"], d["covers"][idx]["mime"])


CACHE_MISS_DAYS = 14


def _cached(cache, key, fn):
    hit = cache.get(key)
    if hit is not None and (hit.get("found") or time.time() - hit.get("t", 0) < CACHE_MISS_DAYS * 86400):
        return hit.get("info")
    info = fn()  # 네트워크 오류는 예외로 올라가 캐시되지 않는다
    cache[key] = {"found": bool(info), "info": info, "t": time.time()}
    return info


def needs_mb(g):
    """Deezer 결과만으로 확정할 수 없어 MusicBrainz 확인이 필요한 그룹인지."""
    if not g.get("dz"):
        return True  # Deezer에서 못 찾음
    members = g["members"]
    if any("cover" in m["missing"] for m in members):
        dz = g["dz"]
        if not g.get("dz_cover"):
            return True  # 커버를 못 받음
        if not (dz["kind"] == "album" and dz["score"] >= 0.95):
            return True  # 곡 단위로 찾았거나 앨범 이름이 정확히 맞지 않음 → 교차 확인
    return False


def online_fill(sugs, http, cache, library_genres, progress=None, stop=None, workers=6, on_update=None,
                dz_workers=4):
    """빈 앨범·장르·없는 커버를 채운다.
    1단계: Deezer를 여러 개 동시에 검색(요청 간격은 공유 제한으로 지킴)하고 커버도 병렬로 내려받는다.
    2단계: Deezer만으로 확정되지 않은 그룹만 MusicBrainz(초당 1회)로 교차 확인한다.
    앨범 태그가 있으면 앨범마다 한 번만 찾는다."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    groups = group_sugs(sugs)
    state = {"done": 0, "total": len(groups), "phase": "Deezer"}

    def tick():
        state["done"] += 1
        if progress:
            progress(state["done"], state["total"], state["phase"])

    def needs_cover(g):
        return any("cover" in m["missing"] for m in g["members"])

    def wants_genre(g):
        return bool(library_genres) and any("genre" in m["missing"] for m in g["members"])

    def dz_task(g):
        first = g["members"][0]
        art, title, length = first["final_artist"], first["final_title"], first["length"]
        try:
            if g["album"]:
                info = _cached(cache, "dz|" + g["key"], lambda: dz_lookup_album(http, art, g["album"]))
            else:
                info = _cached(cache, "dz|" + g["key"], lambda: dz_lookup_track(http, title, art, length))
            if info and wants_genre(g):
                try:  # 장르를 못 가져와도 앨범·커버 결과는 살린다
                    g["dz_genres"] = _cached(cache, f"dzg|{info.get('album_id')}|{info.get('genre_id')}",
                                             lambda: dz_genre_names(http, info.get("album_id"),
                                                                    info.get("genre_id"))) or []
                except Exception:
                    g["dz_genres"] = []
            g["dz"] = info
            if info and needs_cover(g):
                g["dz_cover"] = download_cover(http, info)
        except Exception as e:
            first["error"] = str(e)
            g["dz"] = None
        apply_group(g, library_genres)
        return g

    # 1단계: Deezer (병렬)
    with ThreadPoolExecutor(max_workers=dz_workers) as pool:
        futs = []
        for g in groups:
            if stop and stop.is_set():
                break
            futs.append(pool.submit(dz_task, g))
        for f in as_completed(futs):
            f.result()
            tick()
            if on_update:
                on_update()
        if stop and stop.is_set():
            pool.shutdown(cancel_futures=True)

    # 2단계: 필요한 그룹만 MusicBrainz
    mb_groups = [g for g in groups if needs_mb(g)] if not (stop and stop.is_set()) else []
    state["phase"], state["total"] = "MusicBrainz", state["done"] + len(mb_groups)
    if progress:
        progress(state["done"], state["total"], state["phase"])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = []
        for g in mb_groups:
            if stop and stop.is_set():
                break
            first = g["members"][0]
            art, title, length = first["final_artist"], first["final_title"], first["length"]
            try:
                if g["album"]:
                    info = _cached(cache, "mb|" + g["key"], lambda: mb_lookup_album(http, art, g["album"]))
                else:
                    info = _cached(cache, "mb|" + g["key"], lambda: mb_lookup_track(http, title, art, length))
            except Exception as e:
                first["error"] = str(e)
                info = None
            g["mb"] = info
            if info and needs_cover(g):
                pending.append((g, pool.submit(download_cover, http, info)))
            apply_group(g, library_genres)
            for item in list(pending):
                if item[1].done():
                    item[0]["mb_cover"] = item[1].result()
                    pending.remove(item)
                    apply_group(item[0], library_genres)
            tick()
            if on_update:
                on_update()
        for g, fut in pending:
            try:
                g["mb_cover"] = fut.result()
            except Exception:
                g["mb_cover"] = None
            apply_group(g, library_genres)
    return {"groups": len(groups), "mb_checked": len(mb_groups)}


def count_online_groups(sugs):
    return len(group_sugs(sugs))


def set_cover_choice(sug, idx):
    opts = sug.get("cover_options") or []
    if 0 <= idx < len(opts):
        sug["cover_choice"] = idx
        sug["cover"] = (opts[idx]["data"], opts[idx]["mime"])


def has_anything(sug, fmt=None, use_name=True):
    return bool(sug["changes"] or sug["cover"] or (use_name and fmt and filename_for(sug, fmt)))


def summarize(sug, fmt=None, use_name=True, use_cover=True):
    parts = [f"{FIELD_LABEL[f]}: {old or '(없음)'} → {new}" for f, (old, new) in sug["changes"].items()]
    if use_cover and sug["cover"]:
        parts.append("커버 추가")
    name = filename_for(sug, fmt) if (use_name and fmt) else None
    if name:
        parts.append(f"파일명 → {name}")
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
        data, mime = compact_cover(*sug["cover"])
        tags.add(APIC(encoding=1, mime=mime, type=3, desc="Cover", data=data))
        backup["cover_added"] = True
        changed = True
    if changed:
        # Windows 탐색기 호환을 위해 ID3v2.3으로 저장. 여유 공간(padding)을 넉넉히 두어
        # 다음에 태그를 고칠 때는 파일 전체를 다시 쓰지 않아도 되게 한다.
        tags.save(path, v2_version=3, padding=_padding)

    if use_name and sug["new_name"]:
        with _RENAME_LOCK:  # 여러 파일을 동시에 처리할 때 같은 새 이름을 두 번 고르지 않게
            target = _unique_path(path.with_name(sug["new_name"]), path)
            if target != path:
                os.rename(path, target)
                backup["new_path"] = str(target)
    return backup


_RENAME_LOCK = __import__("threading").Lock()


def _padding(info):
    """기존 여유 공간에 들어가면 그대로 둬서 파일을 다시 쓰지 않고, 넘치면 16KB 여유를 두고 한 번만 다시 쓴다."""
    return info.padding if info.padding >= 0 else 16384


def compact_cover(data, mime, max_px=600, max_bytes=200_000):
    """커버가 지나치게 크면 600px·JPEG로 줄인다(파일마다 쓰는 양을 줄여 적용을 빠르게)."""
    if len(data) <= max_bytes:
        return data, mime
    img, _ = _load_image(data)
    if img is None:
        return data, mime
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue(), "image/jpeg"


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
