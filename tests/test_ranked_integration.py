"""Ranked mode wiring (fork): the index-only user refresh, the bulk-run hook and the webhook."""

import os
import types

import pytest

import artwork_uploader
import core.globals as app_globals
from models.callbacks import ProcessingCallbacks
from models.options import Options
from scrapers.theposterdb_scraper import ThePosterDBScraper
from services import ranked_service
from services.asset_index import AssetIndex
from services.ranked_service import RankedStore


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "config", exist_ok=True)
    monkeypatch.chdir(tmp_path)


def _asset(asset_id):
    return {"id": asset_id, "title": f"Film {asset_id}", "year": 2020, "season": None,
            "media_type": "movie_poster", "author": "someone",
            "url": f"https://theposterdb.com/api/assets/{asset_id}"}


# --- refresh_index ---------------------------------------------------------------------------

def test_refresh_index_records_new_uploads_without_hydrating(monkeypatch):
    scraper = ThePosterDBScraper("https://theposterdb.com/user/Someone", ProcessingCallbacks())
    scraper.set_options(Options())
    monkeypatch.setattr("utils.soup_utils.cook_soup", lambda url: object())
    monkeypatch.setattr(scraper, "get_set_author", lambda soup: setattr(scraper, "author", "Someone"))

    def user_info():
        scraper.user_uploads = 2
        scraper.user_pages = 1
    monkeypatch.setattr(scraper, "scrape_user_info", user_info)

    def page(page_no, catalog=None):
        if page_no == 0:
            catalog.extend([_asset(1), _asset(2)])
        return True
    monkeypatch.setattr(scraper, "scrape_user_page", page)
    monkeypatch.setattr(scraper, "_hydrate_from_cache", lambda *a: pytest.fail("must not hydrate"))

    assert scraper.refresh_index() == 2
    assert {row["asset_id"] for row in AssetIndex().assets_for_user("someone")} == {1, 2}


def test_refresh_index_refuses_non_user_urls():
    from core.exceptions import ScraperException
    scraper = ThePosterDBScraper("https://theposterdb.com/set/123", ProcessingCallbacks())
    with pytest.raises(ScraperException):
        scraper.refresh_index()


# --- bulk-run hook ---------------------------------------------------------------------------

def _line(url):
    return types.SimpleNamespace(url=url, options=Options())


@pytest.mark.parametrize("dry_run", [True, False])
def test_bulk_ranked_pass_ranks_in_file_order_and_leaves_other_lines(monkeypatch, dry_run):
    monkeypatch.setattr(app_globals, "config", types.SimpleNamespace(
        kometa_base="/k", tpdb_filters=["movie_poster"], stage_assets=True, ranked_dry_run=dry_run,
        upload_retry_attempts=1, upload_retry_backoff_seconds=0), raising=False)
    monkeypatch.setattr(app_globals, "docker", False, raising=False)
    monkeypatch.setattr(app_globals, "plex", types.SimpleNamespace(movie_libraries=[], tv_libraries=[]), raising=False)
    monkeypatch.setattr(artwork_uploader, "update_log", lambda instance, message: None)
    monkeypatch.setattr(artwork_uploader, "notify_web", lambda *a, **k: None)
    refreshed = []
    monkeypatch.setattr(ThePosterDBScraper, "refresh_index", lambda self: refreshed.append(self.url) or 0)

    seen = {}

    class FakeService:
        def __init__(self, movie_libs, tv_libs, user_keys, index, callbacks, **kwargs):
            seen["users"] = user_keys
            seen["dry_run"] = kwargs["dry_run"]
            seen["kometa_base"] = kwargs["kometa_base"]

        def run(self):
            return types.SimpleNamespace(slots_considered=3)

    monkeypatch.setattr(ranked_service, "RankedService", FakeService)

    lines = [_line("https://theposterdb.com/user/DarkMatte"), _line("https://mediux.pro/sets/1"),
             _line("https://theposterdb.com/user/mapas01")]
    tally = ProcessingCallbacks()
    remaining, errors = artwork_uploader.run_ranked_pass(types.SimpleNamespace(), lines, tally)

    assert seen["users"] == ["darkmatte", "mapas01"]
    assert seen["dry_run"] is dry_run and seen["kometa_base"] == "/k"
    # A dry run changes nothing, so the other lines are not left for the normal (writing) loop.
    assert [l.url for l in remaining] == ([] if dry_run else ["https://mediux.pro/sets/1"])
    assert errors == 0
    assert RankedStore(AssetIndex().path).order() == ["darkmatte", "mapas01"]
    assert tally.assets_processed[0] == 3
    assert len(refreshed) == 2


def test_bulk_ranked_pass_without_a_kometa_folder_changes_nothing(monkeypatch):
    monkeypatch.setattr(app_globals, "config", types.SimpleNamespace(kometa_base="", ranked_dry_run=False), raising=False)
    monkeypatch.setattr(app_globals, "docker", False, raising=False)
    logs = []
    monkeypatch.setattr(artwork_uploader, "update_log", lambda instance, message: logs.append(message))
    monkeypatch.setattr(ranked_service, "RankedService", lambda *a, **k: pytest.fail("must not run"))

    remaining, errors = artwork_uploader.run_ranked_pass(
        types.SimpleNamespace(), [_line("https://theposterdb.com/user/DarkMatte")], ProcessingCallbacks())

    assert errors == 1 and remaining == []
    assert "Kometa asset directory" in logs[0]


def test_dry_run_bulk_file_without_user_urls_runs_nothing(monkeypatch):
    monkeypatch.setattr(app_globals, "config", types.SimpleNamespace(kometa_base="/k", ranked_dry_run=True), raising=False)
    monkeypatch.setattr(artwork_uploader, "update_log", lambda instance, message: None)
    monkeypatch.setattr(ranked_service, "RankedService", lambda *a, **k: pytest.fail("must not run"))

    remaining, errors = artwork_uploader.run_ranked_pass(
        types.SimpleNamespace(), [_line("https://mediux.pro/sets/1")], ProcessingCallbacks())

    assert remaining == [] and errors == 0


# --- webhook ---------------------------------------------------------------------------------

def test_webhook_uses_ranked_path_only_when_live(monkeypatch):
    from services.webhook_service import WebhookService
    monkeypatch.setattr(app_globals, "config", types.SimpleNamespace(ranked_mode=True, ranked_dry_run=True), raising=False)
    assert WebhookService._ranked_enabled() is False
    assert WebhookService._ranked_dry_run() is True       # dry run: imports are not processed at all
    app_globals.config.ranked_dry_run = False
    assert WebhookService._ranked_enabled() is True
    assert WebhookService._ranked_dry_run() is False
    app_globals.config.ranked_mode = False
    assert WebhookService._ranked_enabled() is False
    assert WebhookService._ranked_dry_run() is False      # ranked mode off: upstream behaviour
