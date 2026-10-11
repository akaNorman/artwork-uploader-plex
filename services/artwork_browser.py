"""
Artwork tab (fork addition, ranked mode).

Lists every film and show that has posters from the ranked users, and for each one its main
poster and season slots with every option, best-ranked first. Picking an option pins it (for the
item in every library that holds it) and applies it straight away; unpinning hands the slot back
to the ranking. The daily ranked run honours pins.

The listing is built from what's cached - the Plex libraries listed once, and the users' cached
posters matched in memory - and never reads a ThePosterDB page. Thumbnails are made the first time
a poster is shown (one download each, politely paced) and kept in config/artwork_thumbs.
"""

import io
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from core import globals
from core.constants import ASSET_INDEX_PATH, IMAGE_EXTENSIONS
from core.enums import MediaType
from models.callbacks import ProcessingCallbacks
from models.instance import Instance
from services.asset_index import AssetIndex
from services.ranked_service import (POSTER_SLOT, Candidate, RankedBusy, RankedService, RankedStore,
                                     TPDbClient, _md5_bytes, slot_file_stem, slot_label)

THUMBS_DIR = os.path.join(os.path.dirname(ASSET_INDEX_PATH), "artwork_thumbs")
THUMB_SIZE = (240, 360)
REFRESH_AFTER_SECONDS = 30 * 60
SLOT_PATTERN = re.compile(r"^(poster|season\d{1,3})$")


class ArtworkBrowserError(Exception):
    """A request the Artwork tab can't serve; the message is shown to the user."""


def _kometa_base() -> str:
    return "/assets" if globals.docker else (getattr(globals.config, "kometa_base", "") or "")


def _slot_order(slot: str) -> Tuple[int, int]:
    return (0, 0) if slot == POSTER_SLOT else (1, int(slot[len("season"):]))


def _log(message: str) -> None:
    from utils.notifications import update_log
    update_log(Instance(broadcast=True), message)


def _make_thumbnail(data: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail(THUMB_SIZE)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()


class ArtworkBrowser:

    def __init__(self, thumbs_dir: str = THUMBS_DIR, service_factory=None, tpdb: Optional[TPDbClient] = None) -> None:
        self.thumbs_dir = thumbs_dir
        self._service_factory = service_factory or self._default_service
        self._service: Optional[RankedService] = None
        self._built_at = 0.0
        self._build_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._tpdb = tpdb
        self._items: List[dict] = []
        self._urls: Dict[int, str] = {}

    # --- the listing ---------------------------------------------------------------------------
    @staticmethod
    def _default_service() -> RankedService:
        config = globals.config
        if getattr(config, "ranked_mode", False) is not True:
            raise ArtworkBrowserError("Ranked mode is off - the Artwork tab picks between the ranked users' posters")
        if globals.plex is None or not globals.plex.ensure_libraries():
            raise ArtworkBrowserError("Plex isn't set up yet")
        index = AssetIndex()
        store = RankedStore(index.path)
        user_keys = store.order()
        if not user_keys:
            raise ArtworkBrowserError("Run your bulk file of ThePosterDB users once in ranked mode first - that sets the ranking")
        return RankedService(
            globals.plex.movie_libraries, globals.plex.tv_libraries, user_keys, index,
            ProcessingCallbacks(on_log_update=_log),
            file_types=config.tpdb_filters, kometa_base=_kometa_base(), stage_assets=config.stage_assets,
            dry_run=True, store=store, retry_attempts=config.upload_retry_attempts,
            retry_backoff=config.upload_retry_backoff_seconds, write_report=False, quiet=True, offline=True)

    def service(self, refresh: bool = False) -> RankedService:
        with self._build_lock:
            stale = time.time() - self._built_at > REFRESH_AFTER_SECONDS
            if self._service is None or refresh or stale:
                service = self._service_factory()
                service.prepare()
                self._service = service
                self._built_at = time.time()
                self._items = self._build_items(service)
            return self._service

    def _build_items(self, service: RankedService) -> List[dict]:
        per_item: Dict[Tuple[str, int], Dict[str, List[Candidate]]] = {}
        self._urls = {}
        for (kind, tmdb_id, slot), candidates in service._groups.items():
            per_item.setdefault((kind, tmdb_id), {})[slot] = candidates
            for c in candidates:
                self._urls[c.asset_id] = c.row["url"]
        pins = service._pins
        items = []
        for (kind, tmdb_id), slots in per_item.items():
            entries = service._by_tmdb.get((kind, tmdb_id), [])
            if not entries:
                continue
            first = entries[0]
            items.append({
                "kind": kind, "tmdb_id": tmdb_id, "title": first.get("title"), "year": first.get("year"),
                "libraries": sorted({e["library"] for e in entries}),
                "slots": len(slots),
                "options": sum(len(c) for c in slots.values()),
                "choice": any(len(c) > 1 for c in slots.values()),
                "pinned": sum(1 for slot in slots if (kind, tmdb_id, slot) in pins),
            })
        items.sort(key=lambda i: ((i["title"] or "").casefold(), i["year"] or 0))
        return items

    def items(self, refresh: bool = False) -> List[dict]:
        self.service(refresh)
        return self._items

    def item(self, kind: str, tmdb_id: int) -> dict:
        service = self.service()
        entries = service._by_tmdb.get((kind, tmdb_id), [])
        if not entries:
            raise ArtworkBrowserError("That item isn't in your libraries any more - refresh the list")
        applied = service.store.all_applied()
        pins = service.store.pins()
        slots = []
        for (k, t, slot), candidates in service._groups.items():
            if k != kind or t != tmdb_id:
                continue
            current = {e["library"]: (applied.get((e["library"], int(e["rating_key"]), slot)) or {}) for e in entries}
            current_ids = {lib: (row["asset_id"] if row else None) for lib, row in current.items()}
            options = []
            for c in service.ranked(candidates):
                options.append({
                    "asset_id": c.asset_id,
                    "user": c.row["author"] or c.row["user_key"],
                    "rank": c.rank + 1,
                    "title": c.row["title"], "year": c.row["year"],
                    "poster_page": f"https://theposterdb.com/poster/{c.asset_id}",
                    "current_in": sorted(lib for lib, aid in current_ids.items() if aid == c.asset_id),
                })
            slots.append({
                "slot": slot, "label": slot_label(slot),
                "pinned": pins.get((kind, tmdb_id, slot)),
                "options": options,
            })
        slots.sort(key=lambda s: _slot_order(s["slot"]))
        return {
            "kind": kind, "tmdb_id": tmdb_id, "title": entries[0].get("title"), "year": entries[0].get("year"),
            "libraries": [{"library": e["library"], "rating_key": int(e["rating_key"])} for e in entries],
            "slots": slots,
            "dry_run": getattr(globals.config, "ranked_dry_run", True) is not False,
        }

    # --- picking -------------------------------------------------------------------------------
    def _candidates(self, service: RankedService, kind: str, tmdb_id: int, slot: str) -> List[Candidate]:
        if not SLOT_PATTERN.match(slot or ""):
            raise ArtworkBrowserError("Unknown slot")
        candidates = service._groups.get((kind, tmdb_id, slot))
        if not candidates:
            raise ArtworkBrowserError("No posters are known for that slot - refresh the list")
        return candidates

    def pin(self, kind: str, tmdb_id: int, slot: str, asset_id: int) -> dict:
        service = self.service()
        candidates = self._candidates(service, kind, tmdb_id, slot)
        chosen = next((c for c in candidates if c.asset_id == asset_id), None)
        if chosen is None:
            raise ArtworkBrowserError("That poster isn't one of this slot's options")
        service.store.set_pin(kind, tmdb_id, slot, asset_id)
        label = f"{self._name(service, kind, tmdb_id)} • {slot_label(slot)}"
        _log(f"📌 Artwork • {label} pinned to {chosen.row['author'] or chosen.row['user_key']} (poster {asset_id})")
        return self._apply(service, kind, tmdb_id)

    def unpin(self, kind: str, tmdb_id: int, slot: str) -> dict:
        service = self.service()
        self._candidates(service, kind, tmdb_id, slot)
        service.store.delete_pin(kind, tmdb_id, slot)
        _log(f"📌 Artwork • {self._name(service, kind, tmdb_id)} • {slot_label(slot)} unpinned - back to the ranking")
        return self._apply(service, kind, tmdb_id)

    def _apply(self, service: RankedService, kind: str, tmdb_id: int) -> dict:
        self._items = self._build_items_with_fresh_pins(service)
        if getattr(globals.config, "ranked_dry_run", True) is not False:
            return {"applied": False, "message": "Saved. Ranked mode dry run is on, so nothing was changed - "
                                                 "it's applied once the dry run is off"}
        service.dry_run = False
        try:
            result = service.apply_item(kind, tmdb_id, wait=False)
        except RankedBusy:
            return {"applied": False, "message": "Saved. A ranked run is in progress, so the next daily run "
                                                 "applies it (or pick it again once the run has finished)"}
        finally:
            service.dry_run = True
        changed = result.changed
        if result.failed:
            return {"applied": False, "message": f"Saved, but {result.failed} change(s) failed - see the log; "
                                                 f"the next daily run tries again"}
        return {"applied": True, "changed": changed,
                "message": f"Done - {changed} file(s) changed" if changed else "Done - it was already in place"}

    def _build_items_with_fresh_pins(self, service: RankedService) -> List[dict]:
        service._pins = service.store.pins()
        return self._build_items(service)

    @staticmethod
    def _name(service: RankedService, kind: str, tmdb_id: int) -> str:
        entries = service._by_tmdb.get((kind, tmdb_id)) or [{}]
        year = f" ({entries[0].get('year')})" if entries[0].get("year") else ""
        return f"{entries[0].get('title')}{year}"

    # --- thumbnails ----------------------------------------------------------------------------
    def thumbnail(self, asset_id: int) -> bytes:
        path = os.path.join(self.thumbs_dir, f"{int(asset_id)}.jpg")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
        self.service()
        url = self._urls.get(int(asset_id))
        if not url:
            raise ArtworkBrowserError("Unknown poster")
        with self._download_lock:
            if os.path.isfile(path):  # made by another request while this one waited
                with open(path, "rb") as f:
                    return f.read()
            if self._tpdb is None:
                config = globals.config
                self._tpdb = TPDbClient(attempts=config.upload_retry_attempts, backoff=config.upload_retry_backoff_seconds)
            data, _ = self._tpdb.download(url)
        # The full image is in hand anyway: remember its checksum, so the next run needn't fetch it.
        self.service().store.set_md5(int(asset_id), _md5_bytes(data))
        thumb = _make_thumbnail(data)
        os.makedirs(self.thumbs_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(thumb)
        os.replace(tmp, path)
        return thumb

    def current_thumbnail(self, library: str, rating_key: int, slot: str) -> Optional[bytes]:
        """A thumbnail of the file in the Kometa asset folder now, or None if there isn't one."""
        if not SLOT_PATTERN.match(slot or ""):
            raise ArtworkBrowserError("Unknown slot")
        service = self.service()
        entry = next((e for entries in service._by_tmdb.values() for e in entries
                      if e["library"] == library and int(e["rating_key"]) == int(rating_key)), None)
        if entry is None:
            raise ArtworkBrowserError("Unknown item")
        folder = service._asset_folder(entry)
        if not folder:
            return None
        dest_dir = os.path.join(service.kometa_base, library, folder)
        stem = slot_file_stem(slot)
        for ext in IMAGE_EXTENSIONS:
            path = os.path.join(dest_dir, stem + ext)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return _make_thumbnail(f.read())
        return None


browser = ArtworkBrowser()
