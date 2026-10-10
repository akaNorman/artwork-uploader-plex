"""
Ranked mode (fork addition).

The upstream bulk run walks every poster every followed ThePosterDB user has ever uploaded and,
for each one, searches each Plex library for the item it belongs to. In Kometa mode it then keeps
whichever poster happened to land in the asset folder first. That is slow (hours a day) and it
never upgrades to a preferred artist's poster that arrives later.

Ranked mode turns the run around:

1. The followed users' cached indexes are refreshed (only new uploads are fetched).
2. The Plex libraries are listed once (one request per library) - no per-poster Plex searches.
3. Every cached poster is matched to the library in memory, and for each item and each slot
   (the main poster, and each season poster separately) the poster from the highest-ranked user
   wins; within one user, their newest upload wins.
4. Only slots whose winner differs from what was last applied are touched. The winner is written
   to the Kometa asset folder (any file it replaces is moved to a backup folder, never deleted)
   and uploaded to Plex straight away, so Plex updates immediately and Kometa agrees with it.

A dry run makes every decision and writes a CSV report, but changes nothing in Plex or in the
asset folders. Facts it learns about ThePosterDB assets (an asset's TMDb ID, an image's checksum)
are cached either way, so the real run doesn't fetch them again.
"""

import csv
import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core import globals
from core.constants import (ASSET_INDEX_PATH, IMAGE_EXTENSIONS, RANKED_BACKUP_DIR,
                            RANKED_REPORTS_DIR, RANKED_TPDB_REQUEST_DELAY)
from core.enums import FileType, MediaType
from core.exceptions import ScraperException
from core.retry import call_with_retry
from plex.library_index import PlexLibraryIndex
from services.asset_index import normalize_title
from utils import soup_utils
from utils.utils import get_path_parts, normalize_path

RANKED_FILE_TYPES = (FileType.MOVIE_POSTER.value, FileType.SHOW_COVER.value, FileType.SEASON_COVER.value)
POSTER_SLOT = "poster"

# Only one ranked pass may write at a time (the scheduled run and a webhook import could overlap).
_APPLY_LOCK = threading.Lock()

ACTION_NEW = "new"                # nothing in the asset folder yet - save and upload
ACTION_REPLACE = "replace"        # a different poster is there - back it up, save and upload
ACTION_KEEP = "keep"              # the winner is already the file in the folder - just record it
ACTION_STAGE = "stage"            # season not in Plex yet - save to the asset folder only
ACTION_UPLOAD_STAGED = "upload"   # an earlier staged season has arrived in Plex - upload it now
ACTION_SKIP = "skip"              # nothing can be done (season not in Plex and staging off, ...)


def season_slot(season: int) -> str:
    return f"season{season}"


def slot_file_stem(slot: str) -> str:
    """Kometa asset file name (without extension) for a slot."""
    if slot == POSTER_SLOT:
        return "poster"
    return f"Season{int(slot[len('season'):]):02}"


def slot_label(slot: str) -> str:
    if slot == POSTER_SLOT:
        return "Poster"
    season = int(slot[len("season"):])
    return "Specials" if season == 0 else f"Season {season:02}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _md5_file(path: str) -> Optional[str]:
    try:
        digest = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------------------------
# Persistent state (tables live in the same SQLite file as the user asset index)
# ---------------------------------------------------------------------------------------------

class RankedStore:
    """What ranked mode remembers between runs:
       - ranked_asset_meta: per ThePosterDB asset, the TMDb ID from its poster page (fetched at
         most once) and the checksum of its image (downloaded at most once for comparison).
       - ranked_applied: per Plex item and slot, which asset was last applied and whether it has
         reached Plex yet (a staged season hasn't).
       - ranked_order: the ranking used by the last scheduled run, so webhook imports use the
         same order."""

    def __init__(self, path: str = ASSET_INDEX_PATH) -> None:
        self.path = path
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ranked_asset_meta (
                    asset_id     INTEGER PRIMARY KEY,
                    page_checked INTEGER NOT NULL DEFAULT 0,
                    tmdb_id      INTEGER,
                    md5          TEXT,
                    updated_at   TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ranked_applied (
                    library    TEXT    NOT NULL,
                    rating_key INTEGER NOT NULL,
                    slot       TEXT    NOT NULL,
                    asset_id   INTEGER NOT NULL,
                    user_key   TEXT,
                    md5        TEXT,
                    file_path  TEXT,
                    uploaded   INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT,
                    PRIMARY KEY (library, rating_key, slot)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ranked_order (
                    position INTEGER PRIMARY KEY,
                    user_key TEXT NOT NULL
                )""")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    # --- asset facts -------------------------------------------------------------------------
    def all_meta(self) -> Dict[int, sqlite3.Row]:
        conn = self._connect()
        try:
            return {row["asset_id"]: row for row in conn.execute("SELECT * FROM ranked_asset_meta")}
        finally:
            conn.close()

    def set_page_result(self, asset_id: int, tmdb_id: Optional[int]) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO ranked_asset_meta (asset_id, page_checked, tmdb_id, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET page_checked=1, tmdb_id=excluded.tmdb_id,
                    updated_at=excluded.updated_at""", (asset_id, tmdb_id, _now()))
            conn.commit()
        finally:
            conn.close()

    def set_md5(self, asset_id: int, md5: str) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO ranked_asset_meta (asset_id, md5, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET md5=excluded.md5, updated_at=excluded.updated_at""",
                (asset_id, md5, _now()))
            conn.commit()
        finally:
            conn.close()

    # --- what has been applied ---------------------------------------------------------------
    def all_applied(self) -> Dict[Tuple[str, int, str], sqlite3.Row]:
        conn = self._connect()
        try:
            return {(row["library"], row["rating_key"], row["slot"]): row
                    for row in conn.execute("SELECT * FROM ranked_applied")}
        finally:
            conn.close()

    def set_applied(self, library: str, rating_key: int, slot: str, asset_id: int, user_key: str,
                    md5: Optional[str], file_path: Optional[str], uploaded: bool) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO ranked_applied
                    (library, rating_key, slot, asset_id, user_key, md5, file_path, uploaded, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library, rating_key, slot) DO UPDATE SET
                    asset_id=excluded.asset_id, user_key=excluded.user_key, md5=excluded.md5,
                    file_path=excluded.file_path, uploaded=excluded.uploaded,
                    applied_at=excluded.applied_at""",
                (library, rating_key, slot, asset_id, user_key, md5, file_path, 1 if uploaded else 0, _now()))
            conn.commit()
        finally:
            conn.close()

    # --- ranking -----------------------------------------------------------------------------
    def set_order(self, user_keys: List[str]) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM ranked_order")
            conn.executemany("INSERT INTO ranked_order (position, user_key) VALUES (?, ?)",
                             list(enumerate(user_keys)))
            conn.commit()
        finally:
            conn.close()

    def order(self) -> List[str]:
        conn = self._connect()
        try:
            return [row["user_key"] for row in conn.execute("SELECT user_key FROM ranked_order ORDER BY position")]
        finally:
            conn.close()


# ---------------------------------------------------------------------------------------------
# ThePosterDB access (poster pages for TMDb IDs, image downloads), politely paced
# ---------------------------------------------------------------------------------------------

class TPDbClient:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    }

    def __init__(self, delay: float = RANKED_TPDB_REQUEST_DELAY, timeout: int = 10,
                 attempts: int = 3, backoff: float = 1.0) -> None:
        self.delay = delay
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self._last = 0.0
        self.page_requests = 0
        self.image_requests = 0

    def _pace(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def tmdb_id(self, asset_id: int) -> Tuple[bool, Optional[int]]:
        """(page_read, tmdb_id). page_read is False when the page couldn't be fetched, so the
           caller doesn't cache a network failure as 'no TMDb ID'."""
        self._pace()
        self.page_requests += 1
        try:
            soup = soup_utils.cook_soup(f"https://theposterdb.com/poster/{asset_id}")
        except ScraperException:
            return False, None
        try:
            return True, int(soup.find('div', {"data-media-id": True})['data-media-id'])
        except (KeyError, TypeError, ValueError):
            return True, None

    def download(self, url: str) -> Tuple[bytes, str]:
        """(image bytes, file extension)."""
        self._pace()
        self.image_requests += 1

        def _fetch():
            response = requests.get(url, headers=self.HEADERS, timeout=self.timeout)
            response.raise_for_status()
            return response

        response, _ = call_with_retry(_fetch, self.attempts, self.backoff)
        content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if ext in (None, ".jpe", ".jpeg"):
            ext = ".jpg" if ext != ".jpeg" else ".jpeg"
        if ext not in IMAGE_EXTENSIONS:
            ext = ".jpg"
        return response.content, ext


# ---------------------------------------------------------------------------------------------
# The ranked pass
# ---------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    rank: int
    asset_id: int
    row: sqlite3.Row
    verified: bool


@dataclass
class Decision:
    action: str
    entry: dict
    slot: str
    candidate: Candidate
    dest_dir: str
    existing: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class RankedResult:
    dry_run: bool
    counts: Dict[str, int] = field(default_factory=dict)
    by_user: Dict[str, int] = field(default_factory=dict)
    failed: int = 0
    slots_considered: int = 0
    assets_considered: int = 0
    report_path: Optional[str] = None
    page_requests: int = 0
    image_requests: int = 0
    stopped: bool = False

    @property
    def changed(self) -> int:
        return sum(self.counts.get(a, 0) for a in (ACTION_NEW, ACTION_REPLACE, ACTION_STAGE, ACTION_UPLOAD_STAGED))


class RankedService:

    def __init__(
        self,
        movie_libraries: List,
        tv_libraries: List,
        user_keys: List[str],
        asset_index,
        callbacks,
        *,
        file_types: Optional[List[str]] = None,
        kometa_base: str = "/assets",
        stage_assets: bool = True,
        dry_run: bool = True,
        store: Optional[RankedStore] = None,
        tpdb: Optional[TPDbClient] = None,
        retry_attempts: int = 3,
        retry_backoff: float = 1.0,
        backup_root: str = RANKED_BACKUP_DIR,
        reports_dir: str = RANKED_REPORTS_DIR,
        report_label: str = "ranked",
        write_report: bool = True,
        quiet: bool = False,
    ) -> None:
        self.movie_libraries = movie_libraries or []
        self.tv_libraries = tv_libraries or []
        # A user listed twice keeps their first (higher) position.
        self.user_keys = list(dict.fromkeys(key.casefold() for key in user_keys if key))
        self.rank = {key: i for i, key in enumerate(self.user_keys)}
        self.asset_index = asset_index
        self.callbacks = callbacks
        wanted = file_types if file_types else list(RANKED_FILE_TYPES)
        self.file_types = [t for t in wanted if t in RANKED_FILE_TYPES]
        self.kometa_base = kometa_base
        self.stage_assets = stage_assets
        self.dry_run = dry_run
        self.store = store or RankedStore(getattr(asset_index, "path", ASSET_INDEX_PATH))
        self.tpdb = tpdb or TPDbClient(attempts=retry_attempts, backoff=retry_backoff)
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        self.backup_root = backup_root
        self.reports_dir = reports_dir
        self.report_label = report_label
        self.write_report = write_report
        self.quiet = quiet  # single-title (webhook) runs: no run summary in the log
        self.run_stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        self._library_index: Optional[PlexLibraryIndex] = None
        self._by_tmdb: Dict[Tuple[str, int], List[dict]] = {}
        self._meta: Dict[int, sqlite3.Row] = {}
        self._applied: Dict[Tuple[str, int, str], sqlite3.Row] = {}
        self._folders: Dict[Tuple[str, int], Optional[str]] = {}
        # (library, asset folder) -> the different items (TMDb ID, or rating key without one) using it
        self._folder_owners: Dict[Tuple[str, str], set] = {}
        self._seasons: Dict[Tuple[str, int], Dict[int, object]] = {}

    # --- logging helpers ---------------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.callbacks:
            self.callbacks.log(message)

    def _debug(self, message: str) -> None:
        if self.callbacks:
            self.callbacks.debug(message, "RankedService")

    def _progress(self, current: int, total: int, title: str) -> None:
        if self.callbacks:
            self.callbacks.progress(current, total, title)

    # --- entry point -------------------------------------------------------------------------
    def run(self, only_title: Optional[Tuple[str, str, Optional[int]]] = None) -> RankedResult:
        """Run the ranked pass over the whole library, or, with only_title=(kind, title, year),
           over a single title (used for Sonarr/Radarr imports)."""
        with _APPLY_LOCK:
            return self._run(only_title)

    def _run(self, only_title) -> RankedResult:
        result = RankedResult(dry_run=self.dry_run)
        if not self.user_keys or not self.file_types:
            self._log("⚠️ Ranked mode • no ThePosterDB users or artwork types to rank, nothing to do")
            return result

        started = time.time()
        self._build_inventory()
        self._meta = self.store.all_meta()
        self._applied = self.store.all_applied()

        rows = self._load_assets(only_title)
        result.assets_considered = len(rows)
        groups = self._group(rows, result)
        self._debug(f"{len(rows)} cached posters from {len(self.user_keys)} users matched "
                    f"{len(groups)} item slots in your libraries")

        decisions: List[Decision] = []
        total = len(groups)
        for i, (key, candidates) in enumerate(groups.items(), 1):
            if globals.cancel_scrape:
                result.stopped = True
                break
            if i % 25 == 0 or i == total:
                self._progress(i, total, f"Ranked mode • choosing posters • {i} of {total}")
            kind, tmdb_id, slot = key
            chosen = self._choose(tmdb_id, candidates)
            if chosen is None:
                continue
            for entry in self._by_tmdb.get((kind, tmdb_id), []):
                result.slots_considered += 1
                try:
                    decision = self._decide(entry, slot, chosen)
                except Exception as e:
                    result.failed += 1
                    self._log(f"❌ {self._describe(entry, slot)} • could not check the current poster: {e}")
                    continue
                if decision is None:
                    continue
                if not self.dry_run and decision.action != ACTION_SKIP:
                    if not self._execute(decision):
                        result.failed += 1
                        continue
                decisions.append(decision)
                result.counts[decision.action] = result.counts.get(decision.action, 0) + 1
                if decision.action in (ACTION_NEW, ACTION_REPLACE, ACTION_STAGE, ACTION_UPLOAD_STAGED):
                    author = decision.candidate.row["author"] or decision.candidate.row["user_key"]
                    result.by_user[author] = result.by_user.get(author, 0) + 1

        result.page_requests = self.tpdb.page_requests
        result.image_requests = self.tpdb.image_requests
        if self.write_report and decisions:
            result.report_path = self._write_report(decisions)
        if not self.quiet:
            self._summarise(result, time.time() - started)
        return result

    # --- step 1: the libraries, listed once ---------------------------------------------------
    def _build_inventory(self) -> None:
        index = PlexLibraryIndex([], [])
        index.movie_libraries = list(self.movie_libraries)
        index.tv_libraries = list(self.tv_libraries)
        self._by_tmdb = {}
        self._folder_owners = {}
        for library in self.movie_libraries + self.tv_libraries:
            entries = index.index_items(library, library.all())
            for entry in entries:
                if entry["tmdb_id"] is not None:
                    self._by_tmdb.setdefault((entry["type"], entry["tmdb_id"]), []).append(entry)
                folder = self._asset_folder(entry)
                if folder:
                    owner = entry["tmdb_id"] if entry["tmdb_id"] is not None else ("rating_key", entry["rating_key"])
                    self._folder_owners.setdefault((library.title, folder), set()).add(owner)
            self._debug(f"Listed {len(entries)} items from {library.title}")
        self._library_index = index

    # --- step 2: the cached posters of the ranked users ---------------------------------------
    def _load_assets(self, only_title) -> List[sqlite3.Row]:
        rows: List[sqlite3.Row] = []
        title_keys = None
        wanted_types = self.file_types
        if only_title is not None:
            only_kind, title, _ = only_title
            if only_kind == MediaType.MOVIE.value:
                wanted_types = [t for t in wanted_types if t == FileType.MOVIE_POSTER.value]
            else:
                wanted_types = [t for t in wanted_types if t != FileType.MOVIE_POSTER.value]
            title_keys = {normalize_title(title)}
            stripped = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
            if stripped:
                title_keys.add(normalize_title(stripped))
        for user_key in self.user_keys:
            for row in self.asset_index.assets_for_user(user_key):
                if row["media_type"] not in wanted_types:
                    continue
                if title_keys is not None and row["title_key"] not in title_keys:
                    continue
                rows.append(row)
        return rows

    # --- step 3: match each poster to an item and group by item + slot ------------------------
    def _group(self, rows: List[sqlite3.Row], result: RankedResult) -> Dict[Tuple[str, int, str], List[Candidate]]:
        groups: Dict[Tuple[str, int, str], List[Candidate]] = {}
        for row in rows:
            media_type = row["media_type"]
            kind = MediaType.MOVIE.value if media_type == FileType.MOVIE_POSTER.value else MediaType.TV_SHOW.value
            if media_type == FileType.SEASON_COVER.value:
                if row["season"] is None:
                    continue
                slot = season_slot(int(row["season"]))
            else:
                slot = POSTER_SLOT

            resolved = self._resolve(row, kind)
            if resolved is None:
                continue
            tmdb_id, verified = resolved
            if (kind, tmdb_id) not in self._by_tmdb:
                continue
            groups.setdefault((kind, tmdb_id, slot), []).append(
                Candidate(rank=self.rank.get(row["user_key"], len(self.rank)),
                          asset_id=int(row["asset_id"]), row=row, verified=verified))
        return groups

    def _resolve(self, row: sqlite3.Row, kind: str) -> Optional[Tuple[int, bool]]:
        """Which TMDb ID this poster is for, and whether that is confirmed.

           A poster page read earlier is the authority. Otherwise the title is matched against the
           library in memory: a match on the same title AND the same year counts as confirmed; a
           match one year out (or with no year) is only provisional, and the poster page is read
           if and when that poster would actually win. A title that matches several different
           items (same-name remakes) has its poster page read straight away."""
        asset_id = int(row["asset_id"])
        meta = self._meta.get(asset_id)
        if meta is not None and meta["page_checked"] and meta["tmdb_id"] is not None:
            return int(meta["tmdb_id"]), True

        status, match = self._library_index.lookup(row["title"] or "", row["year"], kind)
        if status == "not_found":
            return None
        if status == "ambiguous":
            if meta is not None and meta["page_checked"]:
                return None  # the page had no TMDb ID, and the title alone can't decide
            tmdb_id = self._read_page(asset_id)
            return (tmdb_id, True) if tmdb_id is not None else None

        tmdb_id = match.get("tmdb_id") if match else None
        if tmdb_id is None:
            return None
        if (kind, tmdb_id) not in self._by_tmdb:
            # The index refreshed an entry Plex had only just matched; make it findable.
            self._by_tmdb.setdefault((kind, tmdb_id), []).append(match)
        page_checked_no_id = meta is not None and meta["page_checked"]
        exact_year = row["year"] is not None and match.get("year") == row["year"]
        return int(tmdb_id), bool(exact_year or page_checked_no_id)

    def _read_page(self, asset_id: int) -> Optional[int]:
        page_read, tmdb_id = self.tpdb.tmdb_id(asset_id)
        if page_read:
            self.store.set_page_result(asset_id, tmdb_id)
            self._meta[asset_id] = {"asset_id": asset_id, "page_checked": 1, "tmdb_id": tmdb_id, "md5": None}
        return tmdb_id

    # --- step 4: pick the winner for a slot ---------------------------------------------------
    def _choose(self, tmdb_id: int, candidates: List[Candidate]) -> Optional[Candidate]:
        for candidate in sorted(candidates, key=lambda c: (c.rank, -c.asset_id)):
            if candidate.verified:
                return candidate
            meta = self._meta.get(candidate.asset_id)
            if meta is not None and meta["page_checked"]:
                page_tmdb = meta["tmdb_id"]
            else:
                page_tmdb = self._read_page(candidate.asset_id)
                meta = self._meta.get(candidate.asset_id)
                if meta is None or not meta["page_checked"]:
                    # The page couldn't be fetched this time. Don't guess, and don't fall back
                    # to a lower-ranked poster either (the next run would only swap it back):
                    # leave the slot alone until the page can be read.
                    self._debug(f"Could not read the poster page for asset {candidate.asset_id}; "
                                f"slot left alone this run")
                    return None
            if page_tmdb is None or page_tmdb == tmdb_id:
                return candidate  # no ID on the page: trust the title match, as upstream does
        return None

    # --- step 5: what to do for one item ------------------------------------------------------
    def _decide(self, entry: dict, slot: str, chosen: Candidate) -> Optional[Decision]:
        library = entry["library"]
        rating_key = int(entry["rating_key"])
        applied = self._applied.get((library, rating_key, slot))
        season = None if slot == POSTER_SLOT else int(slot[len("season"):])

        # The common daily case: the winner is what we applied last time. Decided from the
        # database alone, without touching the asset folders.
        if applied is not None and int(applied["asset_id"]) == chosen.asset_id:
            if applied["uploaded"] or season is None:
                return None
            # Staged earlier; upload once the season exists in Plex.
            if self._season_in_plex(entry, season):
                path = applied["file_path"]
                existing = [path] if path and os.path.isfile(path) else []
                dest_dir = os.path.dirname(path) if path else ""
                return Decision(ACTION_UPLOAD_STAGED, entry, slot, chosen, dest_dir, existing)
            return None

        folder = self._asset_folder(entry)
        if not folder:
            raise RuntimeError("could not work out the item's folder name")
        dest_dir = os.path.join(self.kometa_base, library, folder)
        existing = self._existing_files(dest_dir, slot_file_stem(slot))

        # Kometa matches an asset folder by name, so a folder shared by different items (films
        # side by side in one folder, or loose files in the library root) can't be given one
        # item's poster without it landing on the others too. Leave it alone and report it.
        owners = self._folder_owners.get((library, folder), set())
        if len(owners) > 1:
            return Decision(ACTION_SKIP, entry, slot, chosen, dest_dir, existing,
                            note=f"asset folder '{folder}' is shared by {len(owners)} different items in {library} - left alone")

        in_plex = season is None or self._season_in_plex(entry, season)

        chosen_md5 = self._known_md5(chosen.asset_id)
        compare_note = ""
        if existing:
            if chosen_md5 is None:
                chosen_md5 = self._fetch_md5(chosen)
            if chosen_md5 is not None and any(_md5_file(path) == chosen_md5 for path in existing):
                return Decision(ACTION_KEEP, entry, slot, chosen, dest_dir, existing,
                                note="" if in_plex else "season not in Plex yet")
            if chosen_md5 is None:
                compare_note = "could not download the winner to compare - the current file may already be it"
            action = ACTION_REPLACE
        else:
            action = ACTION_NEW

        if not in_plex:
            if self.stage_assets and season != 0:
                note = "season not in Plex yet, saved for Kometa"
                return Decision(ACTION_STAGE, entry, slot, chosen, dest_dir, existing,
                                note=f"{note}; {compare_note}" if compare_note else note)
            return None
        return Decision(action, entry, slot, chosen, dest_dir, existing, note=compare_note)

    def _known_md5(self, asset_id: int) -> Optional[str]:
        meta = self._meta.get(asset_id)
        return meta["md5"] if meta is not None and meta["md5"] else None

    def _fetch_md5(self, candidate: Candidate) -> Optional[str]:
        try:
            data, ext = self._download(candidate)
        except Exception as e:
            self._debug(f"Could not download asset {candidate.asset_id} to compare it: {e}")
            return None
        return _md5_bytes(data)

    def _download(self, candidate: Candidate) -> Tuple[bytes, str]:
        """Download a winner's image, remembering its checksum so a later run (or the real run
           after a dry run) can compare without downloading it again."""
        cached = getattr(self, "_download_cache", None)
        if cached is not None and cached[0] == candidate.asset_id:
            return cached[1], cached[2]
        data, ext = self.tpdb.download(candidate.row["url"])
        md5 = _md5_bytes(data)
        self.store.set_md5(candidate.asset_id, md5)
        meta = dict(self._meta.get(candidate.asset_id) or {"asset_id": candidate.asset_id, "page_checked": 0, "tmdb_id": None})
        meta["md5"] = md5
        self._meta[candidate.asset_id] = meta
        # Keep only the latest image in memory: a slot's items (e.g. the film in both Movies and
        # Movies 4K) are handled one after another, so they share one download.
        self._download_cache = (candidate.asset_id, data, ext)
        return data, ext

    def _asset_folder(self, entry: dict) -> Optional[str]:
        key = (entry["library"], int(entry["rating_key"]))
        if key in self._folders:
            return self._folders[key]
        item = entry.get("item")
        folder = None
        try:
            if entry["type"] == MediaType.MOVIE.value:
                path = normalize_path(item.media[0].parts[0].file)
                folder = get_path_parts(path)[-2]
            else:
                locations = list(getattr(item, "locations", None) or [])
                if not locations:
                    item = self._fetch_item(entry)
                    locations = list(getattr(item, "locations", None) or [])
                if locations:
                    folder = get_path_parts(normalize_path(locations[0]).rstrip("/"))[-1]
                else:
                    # Same derivation as upstream: the first episode's path.
                    episode_path = normalize_path(item.seasons()[0].episodes()[0].media[0].parts[0].file)
                    parts = get_path_parts(episode_path)
                    in_season_folder = parts[-2].lower().startswith(("season", "specials"))
                    folder = parts[-3] if in_season_folder else parts[-2]
        except Exception as e:
            self._debug(f"Could not work out the folder for {entry.get('title')}: {e}")
            folder = None
        self._folders[key] = folder
        return folder

    def _fetch_item(self, entry: dict):
        library = next((lib for lib in self.movie_libraries + self.tv_libraries if lib.title == entry["library"]), None)
        if library is None:
            return entry.get("item")
        return library.fetchItem(int(entry["rating_key"]))

    def _season_in_plex(self, entry: dict, season: int) -> bool:
        return season in self._season_map(entry)

    def _season_map(self, entry: dict) -> Dict[int, object]:
        key = (entry["library"], int(entry["rating_key"]))
        if key not in self._seasons:
            item = entry.get("item")
            try:
                self._seasons[key] = {s.index: s for s in item.seasons()}
            except Exception as e:
                self._debug(f"Could not list the seasons of {entry.get('title')}: {e}")
                self._seasons[key] = {}
        return self._seasons[key]

    @staticmethod
    def _existing_files(dest_dir: str, stem: str) -> List[str]:
        return [os.path.join(dest_dir, stem + ext) for ext in IMAGE_EXTENSIONS
                if os.path.isfile(os.path.join(dest_dir, stem + ext))]

    # --- step 6: doing it ---------------------------------------------------------------------
    def _execute(self, decision: Decision) -> bool:
        entry, slot, chosen = decision.entry, decision.slot, decision.candidate
        library, rating_key = entry["library"], int(entry["rating_key"])
        description = self._describe(entry, slot)
        author = chosen.row["author"] or chosen.row["user_key"]
        try:
            if decision.action == ACTION_SKIP:
                return True  # reported only, never touched
            if decision.action == ACTION_KEEP:
                self.store.set_applied(library, rating_key, slot, chosen.asset_id, chosen.row["user_key"],
                                       self._known_md5(chosen.asset_id),
                                       decision.existing[0] if decision.existing else None,
                                       uploaded=not decision.note)
                return True

            if decision.action == ACTION_UPLOAD_STAGED:
                path = decision.existing[0] if decision.existing else None
                if path is None:
                    folder = self._asset_folder(entry)
                    if not folder:
                        raise RuntimeError("could not work out the item's folder name")
                    decision.dest_dir = os.path.join(self.kometa_base, library, folder)
                    data, ext = self._download(chosen)
                    path = self._write_file(decision, data, ext)
                self._upload(entry, slot, path)
                self.store.set_applied(library, rating_key, slot, chosen.asset_id, chosen.row["user_key"],
                                       self._known_md5(chosen.asset_id), path, uploaded=True)
                self._log(f"✅ {description} • {author} | {slot_label(slot)} uploaded now the season is in Plex")
                if self.callbacks:
                    self.callbacks.success(1)
                return True

            data, ext = self._download(chosen)
            path = self._write_file(decision, data, ext)
            uploaded = False
            if decision.action != ACTION_STAGE:
                self._upload(entry, slot, path)
                uploaded = True
            self.store.set_applied(library, rating_key, slot, chosen.asset_id, chosen.row["user_key"],
                                   _md5_bytes(data), path, uploaded=uploaded)
            verb = {ACTION_NEW: "added", ACTION_REPLACE: "replaced", ACTION_STAGE: "saved for Kometa (season not in Plex yet)"}[decision.action]
            self._log(f"✅ {description} • {author} | {slot_label(slot)} {verb}")
            if self.callbacks:
                self.callbacks.success(1)
            return True
        except Exception as e:
            self._log(f"❌ {description} • {author} | {slot_label(slot)} failed: {e}")
            if self.callbacks:
                self.callbacks.failed(1)
            return False

    def _write_file(self, decision: Decision, data: bytes, ext: str) -> str:
        """Move whatever is there now into the backup folder (never delete it), then write the
           new file atomically."""
        stem = slot_file_stem(decision.slot)
        os.makedirs(decision.dest_dir, exist_ok=True)
        dest = os.path.join(decision.dest_dir, stem + ext)
        tmp = dest + ".tmp"
        # The new file is written in full before anything is moved, so a failed write leaves the
        # current poster where it is.
        with open(tmp, "wb") as f:
            f.write(data)
        for old in self._existing_files(decision.dest_dir, stem):
            shutil.move(old, self._backup_path(decision, os.path.basename(old)))
        os.replace(tmp, dest)
        return dest

    def _backup_path(self, decision: Decision, filename: str) -> str:
        """Where a replaced file goes. Never an existing file: a backup must not overwrite an
           earlier backup."""
        backup_dir = os.path.join(self.backup_root, self.run_stamp, decision.entry["library"],
                                  os.path.basename(decision.dest_dir))
        os.makedirs(backup_dir, exist_ok=True)
        target = os.path.join(backup_dir, filename)
        root, ext = os.path.splitext(filename)
        n = 1
        while os.path.exists(target):
            target = os.path.join(backup_dir, f"{root}.{n}{ext}")
            n += 1
        return target

    def _upload(self, entry: dict, slot: str, path: str) -> None:
        if slot == POSTER_SLOT:
            target = entry["item"]
        else:
            target = self._season_map(entry).get(int(slot[len("season"):]))
            if target is None:
                raise RuntimeError("season not found in Plex")
        call_with_retry(lambda: target.uploadPoster(filepath=path), self.retry_attempts, self.retry_backoff)

    # --- reporting ----------------------------------------------------------------------------
    @staticmethod
    def _describe(entry: dict, slot: str) -> str:
        year = f" ({entry.get('year')})" if entry.get("year") else ""
        return f"{entry.get('title')}{year} • {entry.get('library')}" + ("" if slot == POSTER_SLOT else f" • {slot_label(slot)}")

    def _write_report(self, decisions: List[Decision]) -> Optional[str]:
        try:
            os.makedirs(self.reports_dir, exist_ok=True)
            kind = "dry-run" if self.dry_run else "applied"
            path = os.path.join(self.reports_dir, f"{self.report_label}-{kind}-{self.run_stamp}.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["action", "library", "title", "year", "slot", "chosen_user", "rank",
                                 "asset_id", "poster_page", "asset_folder", "current_file", "note"])
                order = {ACTION_REPLACE: 0, ACTION_NEW: 1, ACTION_STAGE: 2, ACTION_UPLOAD_STAGED: 3,
                         ACTION_SKIP: 4, ACTION_KEEP: 5}
                for d in sorted(decisions, key=lambda d: (order.get(d.action, 9), d.entry.get("library") or "",
                                                          d.entry.get("title") or "", d.slot)):
                    writer.writerow([
                        d.action, d.entry.get("library"), d.entry.get("title"), d.entry.get("year"),
                        slot_label(d.slot), d.candidate.row["author"] or d.candidate.row["user_key"],
                        d.candidate.rank + 1, d.candidate.asset_id,
                        f"https://theposterdb.com/poster/{d.candidate.asset_id}", d.dest_dir,
                        os.path.basename(d.existing[0]) if d.existing else "", d.note,
                    ])
            return path
        except OSError as e:
            self._log(f"⚠️ Ranked mode • could not write the report: {e}")
            return None

    def _summarise(self, result: RankedResult, seconds: float) -> None:
        c = result.counts
        mode = "Dry run (nothing changed)" if result.dry_run else "Ranked run"
        verb = "would" if result.dry_run else ""
        parts = [
            f"{c.get(ACTION_REPLACE, 0)} {verb} replace".replace("  ", " "),
            f"{c.get(ACTION_NEW, 0)} {verb} add".replace("  ", " "),
        ]
        if c.get(ACTION_STAGE):
            parts.append(f"{c[ACTION_STAGE]} {verb} stage for seasons not in Plex yet".replace("  ", " "))
        if c.get(ACTION_UPLOAD_STAGED):
            parts.append(f"{c[ACTION_UPLOAD_STAGED]} staged {'would be ' if result.dry_run else ''}uploaded")
        if c.get(ACTION_SKIP):
            parts.append(f"{c[ACTION_SKIP]} left alone (shared asset folder, see the report)")
        parts.append(f"{c.get(ACTION_KEEP, 0)} already right")
        if result.failed:
            parts.append(f"{result.failed} failed")
        minutes = seconds / 60
        self._log(f"🏆 {mode} • {' • '.join(parts)} • {result.page_requests} poster page(s) and "
                  f"{result.image_requests} image(s) fetched • {minutes:.1f} min")
        if result.by_user:
            ranked = sorted(result.by_user.items(), key=lambda kv: self.rank.get(kv[0].casefold(), 99))
            self._log("🏆 Changes by user • " + " • ".join(f"{user}: {n}" for user, n in ranked))
        if result.report_path:
            self._log(f"📄 Report saved to {result.report_path}")
