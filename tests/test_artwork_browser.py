"""Artwork tab (fork): pins beat the ranking in every run, a pick is applied straight away to every
library holding the item, the listing never reads a poster page, and the settings form can't
overwrite schedules or forget the webhook token."""

import io
import os
import types

import pytest

import core.globals as app_globals
from core.enums import MediaType
from services import artwork_browser
from services.artwork_browser import ArtworkBrowser, ArtworkBrowserError
from services.ranked_service import ACTION_REPLACE, RankedService
from test_ranked_service import TPDB, FakeItem, FakeLibrary, FakeTPDb, asset, env, read, service  # noqa: F401

MOVIE = MediaType.MOVIE.value


def _jpeg(colour):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (400, 600), colour).save(out, "JPEG")
    return out.getvalue()


def _alien_setup(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    uhd = FakeItem(2, "Alien", 1979, 348, path="/m4k/Alien (1979)/Alien.mkv")
    libraries = [FakeLibrary("Movies", "movie", [movie]), FakeLibrary("Movies 4K", "movie", [uhd])]
    env.index.record("darkmatte", [asset(100, "Alien", 1979, author="DarkMatte")])
    env.index.record("other", [asset(200, "Alien", 1979, author="Other")])
    tpdb = FakeTPDb(images={f"{TPDB}/100": _jpeg("red"), f"{TPDB}/200": _jpeg("blue")})
    return movie, uhd, libraries, tpdb


# --- pins in a normal run --------------------------------------------------------------------

def test_a_pin_beats_the_ranking_in_a_normal_run(env):
    movie, uhd, libraries, tpdb = _alien_setup(env)
    env.store.set_pin(MOVIE, 348, "poster", 200)

    service(env, libraries, [], ["darkmatte", "other"], tpdb).run()

    assert movie.uploads == [tpdb.images[f"{TPDB}/200"]]
    assert uhd.uploads == [tpdb.images[f"{TPDB}/200"]]


def test_a_pin_to_a_poster_that_has_gone_leaves_the_slot_alone(env):
    movie, uhd, libraries, tpdb = _alien_setup(env)
    env.store.set_pin(MOVIE, 348, "poster", 999)   # no longer among the cached posters

    result = service(env, libraries, [], ["darkmatte", "other"], tpdb).run()

    assert result.counts == {} and movie.uploads == [] and uhd.uploads == []


def test_the_listing_never_reads_a_poster_page(env):
    movie = FakeItem(1, "The Thing", 1982, 1091, path="/m/The Thing (1982)/x.mkv")
    env.index.record("darkmatte", [asset(100, "The Thing", 1983)])  # year one out: would need a page
    tpdb = FakeTPDb(pages={100: 1091})
    svc = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb, offline=True)

    svc.prepare()

    assert tpdb.page_requests == 0 and tpdb.image_requests == 0
    assert (MOVIE, 1091, "poster") in svc._groups


def test_apply_item_applies_a_pin_to_every_library_and_backs_up(env):
    movie, uhd, libraries, tpdb = _alien_setup(env)
    svc = service(env, libraries, [], ["darkmatte", "other"], tpdb)
    svc.run()                                             # DarkMatte's poster everywhere
    svc = service(env, libraries, [], ["darkmatte", "other"], tpdb, offline=True)
    svc.prepare()
    env.store.set_pin(MOVIE, 348, "poster", 200)

    svc.dry_run = False
    result = svc.apply_item(MOVIE, 348)

    assert result.counts == {ACTION_REPLACE: 2}
    blue = tpdb.images[f"{TPDB}/200"]
    assert movie.uploads[-1] == blue and uhd.uploads[-1] == blue
    assert read(os.path.join(env.kometa, "Movies", "Alien (1979)", "poster.jpg")) == blue
    backups = [name for _, _, names in os.walk(env.backup) for name in names]
    assert sorted(backups) == ["poster.jpg", "poster.jpg"]


# --- the Artwork tab's service ---------------------------------------------------------------

@pytest.fixture
def browser(env, monkeypatch, tmp_path):
    movie, uhd, libraries, tpdb = _alien_setup(env)
    monkeypatch.setattr(app_globals, "config", types.SimpleNamespace(
        ranked_mode=True, ranked_dry_run=False, upload_retry_attempts=1, upload_retry_backoff_seconds=0), raising=False)
    monkeypatch.setattr(artwork_browser, "_log", env.logs.append)

    def factory():
        return service(env, libraries, [], ["darkmatte", "other"], tpdb, offline=True,
                       write_report=False, quiet=True)
    b = ArtworkBrowser(thumbs_dir=str(tmp_path / "thumbs"), service_factory=factory, tpdb=tpdb)
    return types.SimpleNamespace(b=b, movie=movie, uhd=uhd, tpdb=tpdb, env=env)


def test_the_list_has_one_entry_per_item_across_libraries(browser):
    items = browser.b.items()

    assert len(items) == 1
    assert items[0]["title"] == "Alien" and items[0]["libraries"] == ["Movies", "Movies 4K"]
    assert items[0]["options"] == 2 and items[0]["choice"] is True and items[0]["pinned"] == 0


def test_an_item_lists_options_best_ranked_first(browser):
    item = browser.b.item(MOVIE, 348)

    poster = item["slots"][0]
    assert poster["label"] == "Poster" and poster["pinned"] is None
    assert [(o["user"], o["rank"]) for o in poster["options"]] == [("DarkMatte", 1), ("Other", 2)]


def test_picking_pins_and_applies_straight_away(browser):
    result = browser.b.pin(MOVIE, 348, "poster", 200)

    assert result["applied"] is True and result["changed"] == 2
    blue = browser.tpdb.images[f"{TPDB}/200"]
    assert browser.movie.uploads == [blue] and browser.uhd.uploads == [blue]
    item = browser.b.item(MOVIE, 348)
    assert item["slots"][0]["pinned"] == 200
    assert item["slots"][0]["options"][1]["current_in"] == ["Movies", "Movies 4K"]
    assert browser.b.items()[0]["pinned"] == 1


def test_unpinning_goes_back_to_the_ranking(browser):
    browser.b.pin(MOVIE, 348, "poster", 200)

    result = browser.b.unpin(MOVIE, 348, "poster")

    assert result["applied"] is True
    red = browser.tpdb.images[f"{TPDB}/100"]
    assert browser.movie.uploads[-1] == red
    assert browser.b.item(MOVIE, 348)["slots"][0]["pinned"] is None


def test_in_a_dry_run_a_pick_is_saved_but_nothing_changes(browser):
    app_globals.config.ranked_dry_run = True

    result = browser.b.pin(MOVIE, 348, "poster", 200)

    assert result["applied"] is False and "dry run" in result["message"]
    assert browser.movie.uploads == [] and browser.uhd.uploads == []
    assert browser.env.store.pins() == {(MOVIE, 348, "poster"): 200}


def test_a_poster_that_is_not_an_option_is_refused(browser):
    with pytest.raises(ArtworkBrowserError):
        browser.b.pin(MOVIE, 348, "poster", 12345)
    with pytest.raises(ArtworkBrowserError):
        browser.b.pin(MOVIE, 348, "../../etc", 100)
    assert browser.env.store.pins() == {}


def test_a_pick_during_a_ranked_run_is_saved_and_says_so(browser):
    from services.ranked_service import _APPLY_LOCK
    browser.b.items()
    with _APPLY_LOCK:
        result = browser.b.pin(MOVIE, 348, "poster", 200)

    assert result["applied"] is False and "in progress" in result["message"]
    assert browser.env.store.pins() == {(MOVIE, 348, "poster"): 200}


def test_thumbnails_are_made_once_and_kept(browser):
    first = browser.b.thumbnail(100)
    requests_after_first = browser.tpdb.image_requests

    second = browser.b.thumbnail(100)

    from PIL import Image
    with Image.open(io.BytesIO(first)) as image:
        assert image.size[0] <= 240 and image.size[1] <= 360
    assert second == first and browser.tpdb.image_requests == requests_after_first
    assert browser.env.store.all_meta()[100]["md5"]   # the checksum is remembered for the next run


def test_the_current_file_is_shown_when_there_is_one(browser):
    assert browser.b.current_thumbnail("Movies", 1, "poster") is None
    browser.b.pin(MOVIE, 348, "poster", 100)

    assert browser.b.current_thumbnail("Movies", 1, "poster")
    with pytest.raises(ArtworkBrowserError):
        browser.b.current_thumbnail("Movies", 1, "../poster")


# --- HTTP routes -----------------------------------------------------------------------------

def test_the_artwork_routes_require_login(web_harness):
    web_harness.config.auth_enabled = True
    client = web_harness.http_client()

    for path in ("/api/artwork/items", "/api/artwork/item?kind=Movie&tmdb_id=1", "/api/artwork/thumb/1",
                 "/api/artwork/current?library=Movies&rating_key=1&slot=poster"):
        response = client.get(path)
        assert response.status_code == 302 and "/login" in response.headers["Location"], path
    assert client.post("/api/artwork/pin", json={}).status_code == 302


def test_pin_only_accepts_json(web_harness):
    response = web_harness.http_client().post("/api/artwork/pin", data="kind=Movie&tmdb_id=1",
                                              content_type="application/x-www-form-urlencoded")
    assert response.status_code == 400


# --- the settings form -----------------------------------------------------------------------

def test_saving_settings_never_touches_schedules():
    from web_routes import clean_settings_form
    form = clean_settings_form({"schedules": [], "enable_webhooks": True, "webhook_token": "abc"})
    assert "schedules" not in form and form["webhook_token"] == "abc"


def test_an_empty_token_with_webhooks_off_keeps_the_saved_token():
    from web_routes import clean_settings_form
    assert "webhook_token" not in clean_settings_form({"enable_webhooks": False, "webhook_token": ""})
    # With webhooks on, an empty token is a real (invalid) value the page already refuses.
    assert clean_settings_form({"enable_webhooks": True, "webhook_token": ""})["webhook_token"] == ""
