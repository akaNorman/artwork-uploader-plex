"""Ranked mode (fork): choose the highest-ranked user's poster per item and slot, write it to the
Kometa asset folder (backing up what it replaces) and upload it to Plex - or, in a dry run, only
report what would change."""

import csv
import hashlib
import os
import types

import pytest

from core.enums import MediaType
from models.callbacks import ProcessingCallbacks
from services.asset_index import AssetIndex
from services.ranked_service import (ACTION_KEEP, ACTION_NEW, ACTION_REPLACE, ACTION_SKIP, ACTION_STAGE,
                                     ACTION_UPLOAD_STAGED, RankedService, RankedStore)

TPDB = "https://theposterdb.com/api/assets"


# --------------------------------------------------------------------------- fakes

class FakeGuid:
    def __init__(self, gid):
        self.id = gid


class FakeSeason:
    def __init__(self, index):
        self.index = index
        self.uploads = []

    def uploadPoster(self, filepath=None, url=None):
        with open(filepath, "rb") as f:
            self.uploads.append(f.read())


class FakeItem:
    def __init__(self, rating_key, title, year, tmdb, path=None, location=None, seasons=()):
        self.ratingKey = rating_key
        self.title = title
        self.year = year
        self.guids = [FakeGuid(f"tmdb://{tmdb}")] if tmdb else []
        self.slug = None
        self.originalTitle = None
        self.media = [types.SimpleNamespace(parts=[types.SimpleNamespace(file=path)])] if path else []
        self.locations = [location] if location else []
        self._seasons = [FakeSeason(i) for i in seasons]
        self.uploads = []

    def seasons(self):
        return list(self._seasons)

    def add_season(self, index):
        self._seasons.append(FakeSeason(index))

    def season_obj(self, index):
        return next(s for s in self._seasons if s.index == index)

    def uploadPoster(self, filepath=None, url=None):
        with open(filepath, "rb") as f:
            self.uploads.append(f.read())


class FakeLibrary:
    def __init__(self, title, kind, items):
        self.title = title
        self.type = kind  # "movie" or "show"
        self.items = items
        self.listings = 0

    def all(self):
        self.listings += 1
        return list(self.items)

    def fetchItem(self, rating_key):
        return next(i for i in self.items if i.ratingKey == rating_key)


class FakeTPDb:
    def __init__(self, images=None, pages=None):
        self.images = images or {}      # url -> bytes
        self.pages = pages or {}        # asset_id -> tmdb id (or None)
        self.page_requests = 0
        self.image_requests = 0

    def tmdb_id(self, asset_id):
        self.page_requests += 1
        if self.pages.get(asset_id) == "unreachable":
            return False, None
        return True, self.pages.get(asset_id)

    def download(self, url):
        self.image_requests += 1
        if url in getattr(self, "fail_once", set()):
            self.fail_once.discard(url)
            raise RuntimeError("429 Too Many Requests")
        return self.images[url], ".jpg"


# --------------------------------------------------------------------------- helpers

def asset(asset_id, title, year, media_type="movie_poster", season=None, author=None):
    return {"id": asset_id, "title": title, "year": year, "season": season,
            "media_type": media_type, "author": author, "url": f"{TPDB}/{asset_id}"}


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "asset_index.db")
    index = AssetIndex(db)
    store = RankedStore(db)
    logs = []
    callbacks = ProcessingCallbacks(on_log_update=logs.append)
    return types.SimpleNamespace(tmp=tmp_path, db=db, index=index, store=store, logs=logs,
                                 callbacks=callbacks, kometa=str(tmp_path / "assets"),
                                 backup=str(tmp_path / "replaced"), reports=str(tmp_path / "reports"))


def service(env, movie_libs, tv_libs, users, tpdb, dry_run=False, **kwargs):
    return RankedService(movie_libs, tv_libs, users, env.index, env.callbacks,
                         kometa_base=env.kometa, dry_run=dry_run, store=env.store, tpdb=tpdb,
                         backup_root=env.backup, reports_dir=env.reports, **kwargs)


def read(path):
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- tests

def test_highest_ranked_user_wins_and_is_written_to_folder_and_plex(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/media/movies/Alien (1979)/Alien.mkv")
    movies = FakeLibrary("Movies", "movie", [movie])
    env.index.record("darkmatte", [asset(100, "Alien", 1979, author="DarkMatte")])
    env.index.record("other", [asset(200, "Alien", 1979, author="Other")])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"darkmatte-alien", f"{TPDB}/200": b"other-alien"})

    result = service(env, [movies], [], ["DarkMatte", "Other"], tpdb).run()

    dest = os.path.join(env.kometa, "Movies", "Alien (1979)", "poster.jpg")
    assert read(dest) == b"darkmatte-alien"
    assert movie.uploads == [b"darkmatte-alien"]
    assert result.counts == {ACTION_NEW: 1}
    assert tpdb.page_requests == 0  # same title and year: no poster page needed


def test_lower_ranked_user_fills_the_gap(env):
    movie = FakeItem(1, "Heat", 1995, 949, path="/m/Heat (1995)/Heat.mkv")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    env.index.record("other", [asset(200, "Heat", 1995, author="Other")])
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"other-heat"})

    service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte", "other"], tpdb).run()

    assert movie.uploads == [b"other-heat"]


def test_newest_upload_wins_within_one_user(env):
    movie = FakeItem(1, "Heat", 1995, 949, path="/m/Heat (1995)/Heat.mkv")
    env.index.record("darkmatte", [asset(100, "Heat", 1995), asset(150, "Heat", 1995)])
    tpdb = FakeTPDb(images={f"{TPDB}/150": b"newer"})

    service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb).run()

    assert movie.uploads == [b"newer"]


def test_dry_run_changes_nothing_and_writes_a_report(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    folder = os.path.join(env.kometa, "Movies", "Alien (1979)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"first-come-first-served")
    env.index.record("darkmatte", [asset(100, "Alien", 1979, author="DarkMatte")])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"darkmatte-alien"})

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb, dry_run=True).run()

    assert read(os.path.join(folder, "poster.jpg")) == b"first-come-first-served"
    assert movie.uploads == []
    assert env.store.all_applied() == {}
    assert not os.path.exists(env.backup)
    assert result.counts == {ACTION_REPLACE: 1}
    with open(result.report_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["action"] == "replace" and rows[0]["chosen_user"] == "DarkMatte"
    assert rows[0]["current_file"] == "poster.jpg"
    # The checksum learnt in the dry run is kept, so the real run needn't download to compare.
    assert env.store.all_meta()[100]["md5"] == hashlib.md5(b"darkmatte-alien").hexdigest()


def test_replace_moves_the_old_file_to_backup_never_deletes(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    folder = os.path.join(env.kometa, "Movies", "Alien (1979)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.png"), "wb") as f:
        f.write(b"old")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"new"})

    svc = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb)
    result = svc.run()

    assert result.counts == {ACTION_REPLACE: 1}
    assert os.listdir(folder) == ["poster.jpg"]  # no stray poster.png left for Kometa to pick
    backup = os.path.join(env.backup, svc.run_stamp, "Movies", "Alien (1979)", "poster.png")
    assert read(backup) == b"old"


def test_identical_file_is_kept_and_not_reuploaded(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    folder = os.path.join(env.kometa, "Movies", "Alien (1979)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"same")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"same"})

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb).run()

    assert result.counts == {ACTION_KEEP: 1}
    assert movie.uploads == []
    assert env.store.all_applied()[("Movies", 1, "poster")]["asset_id"] == 100


def test_second_run_does_nothing_and_fetches_nothing(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    movies = FakeLibrary("Movies", "movie", [movie])
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"img"})
    service(env, [movies], [], ["darkmatte"], tpdb).run()
    tpdb.image_requests = 0

    result = service(env, [movies], [], ["darkmatte"], tpdb).run()

    assert result.changed == 0 and result.counts == {}
    assert tpdb.image_requests == 0 and tpdb.page_requests == 0
    assert movie.uploads == [b"img"]


def test_higher_ranked_user_uploading_later_upgrades_the_poster(env):
    movie = FakeItem(1, "Heat", 1995, 949, path="/m/Heat (1995)/Heat.mkv")
    movies = FakeLibrary("Movies", "movie", [movie])
    env.index.record("other", [asset(200, "Heat", 1995)])
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"clone", f"{TPDB}/300": b"goat"})
    service(env, [movies], [], ["darkmatte", "other"], tpdb).run()

    env.index.record("darkmatte", [asset(300, "Heat", 1995)])  # DarkMatte finally makes one
    result = service(env, [movies], [], ["darkmatte", "other"], tpdb).run()

    assert result.counts == {ACTION_REPLACE: 1}
    assert movie.uploads == [b"clone", b"goat"]
    assert read(os.path.join(env.kometa, "Movies", "Heat (1995)", "poster.jpg")) == b"goat"


def test_each_season_comes_from_the_best_user_who_has_it(env):
    show = FakeItem(10, "Severance", 2022, 95396, location="/tv/Severance (2022)", seasons=(1, 2))
    tv = FakeLibrary("TV", "show", [show])
    env.index.record("darkmatte", [asset(100, "Severance", 2022, "show_cover"),
                                   asset(101, "Severance", 2022, "season_cover", season=1)])
    env.index.record("other", [asset(200, "Severance", 2022, "show_cover"),
                               asset(201, "Severance", 2022, "season_cover", season=1),
                               asset(202, "Severance", 2022, "season_cover", season=2)])
    tpdb = FakeTPDb(images={f"{TPDB}/{i}": f"img{i}".encode() for i in (100, 101, 200, 201, 202)})

    service(env, [], [tv], ["darkmatte", "other"], tpdb).run()

    folder = os.path.join(env.kometa, "TV", "Severance (2022)")
    assert read(os.path.join(folder, "poster.jpg")) == b"img100"
    assert read(os.path.join(folder, "Season01.jpg")) == b"img101"
    assert read(os.path.join(folder, "Season02.jpg")) == b"img202"
    assert show.uploads == [b"img100"]
    assert show.season_obj(1).uploads == [b"img101"]
    assert show.season_obj(2).uploads == [b"img202"]


def test_season_not_in_plex_is_staged_then_uploaded_when_it_arrives(env):
    show = FakeItem(10, "Severance", 2022, 95396, location="/tv/Severance (2022)", seasons=(1,))
    tv = FakeLibrary("TV", "show", [show])
    env.index.record("darkmatte", [asset(102, "Severance", 2022, "season_cover", season=2)])
    tpdb = FakeTPDb(images={f"{TPDB}/102": b"s2"})

    first = service(env, [], [tv], ["darkmatte"], tpdb).run()
    assert first.counts == {ACTION_STAGE: 1}
    assert read(os.path.join(env.kometa, "TV", "Severance (2022)", "Season02.jpg")) == b"s2"

    show.add_season(2)
    tpdb.image_requests = 0
    second = service(env, [], [tv], ["darkmatte"], tpdb).run()
    assert second.counts == {ACTION_UPLOAD_STAGED: 1}
    assert show.season_obj(2).uploads == [b"s2"]
    assert tpdb.image_requests == 0  # uploaded from the file already in the folder


def test_same_film_in_two_libraries_gets_both_with_one_download(env):
    hd = FakeItem(1, "Dune", 2021, 438631, path="/m/Dune (2021)/Dune.mkv")
    uhd = FakeItem(2, "Dune", 2021, 438631, path="/m4k/Dune (2021) 4K/Dune.mkv")
    env.index.record("darkmatte", [asset(100, "Dune", 2021)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"dune"})

    result = service(env, [FakeLibrary("Movies", "movie", [hd]), FakeLibrary("Movies 4K", "movie", [uhd])],
                     [], ["darkmatte"], tpdb).run()

    assert result.counts == {ACTION_NEW: 2}
    assert hd.uploads == [b"dune"] and uhd.uploads == [b"dune"]
    assert os.path.isfile(os.path.join(env.kometa, "Movies 4K", "Dune (2021) 4K", "poster.jpg"))
    assert tpdb.image_requests == 1


def test_year_one_out_is_checked_against_the_poster_page_and_skipped_if_wrong(env):
    movie = FakeItem(1, "The Thing", 1982, 1091, path="/m/The Thing (1982)/x.mkv")
    env.index.record("darkmatte", [asset(100, "The Thing", 1983)])     # page says another film
    env.index.record("other", [asset(200, "The Thing", 1982)])         # exact match
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"right"}, pages={100: 99999})

    service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte", "other"], tpdb).run()

    assert movie.uploads == [b"right"]
    assert tpdb.page_requests == 1
    # The page result is remembered: the next run doesn't read it again.
    tpdb.page_requests = 0
    service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte", "other"], tpdb).run()
    assert tpdb.page_requests == 0


def test_years_further_apart_match_when_the_poster_page_confirms_the_tmdb_id(env):
    # ThePosterDB says 2021 (production), Plex says 2023 (first aired): Everyone Is Doing Great
    show = FakeItem(10, "Everyone Is Doing Great", 2023, 116600, location="/tv/Everyone Is Doing Great", seasons=(1, 2))
    env.index.record("akanorman", [asset(754296, "Everyone Is Doing Great", 2021, "show_cover"),
                                   asset(754297, "Everyone Is Doing Great", 2021, "season_cover", season=1),
                                   asset(754298, "Everyone Is Doing Great", 2021, "season_cover", season=2)])
    tpdb = FakeTPDb(images={f"{TPDB}/{i}": f"img{i}".encode() for i in (754296, 754297, 754298)},
                    pages={754296: 116600, 754297: 116600, 754298: 116600})

    result = service(env, [], [FakeLibrary("TV", "show", [show])], ["akanorman"], tpdb).run()

    assert result.counts == {ACTION_NEW: 3}
    assert show.uploads == [b"img754296"]
    assert show.season_obj(2).uploads == [b"img754298"]
    # Confirmed once: the next run trusts the remembered page and reads nothing.
    tpdb.page_requests = 0
    service(env, [], [FakeLibrary("TV", "show", [show])], ["akanorman"], tpdb).run()
    assert tpdb.page_requests == 0


def test_a_same_title_remake_years_apart_is_never_put_on_the_wrong_film(env):
    dune = FakeItem(1, "Dune", 2021, 438631, path="/m/Dune (2021)/Dune.mkv")
    env.index.record("darkmatte", [asset(100, "Dune", 1984)])          # Lynch's Dune: TMDb 841
    env.index.record("other", [asset(200, "Dune", 2021)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"1984", f"{TPDB}/200": b"2021"}, pages={100: 841})

    service(env, [FakeLibrary("Movies", "movie", [dune])], [], ["darkmatte", "other"], tpdb).run()

    assert dune.uploads == [b"2021"]
    assert tpdb.page_requests == 1


def test_a_title_only_match_whose_page_has_no_tmdb_id_is_not_used(env):
    dune = FakeItem(1, "Dune", 2021, 438631, path="/m/Dune (2021)/Dune.mkv")
    env.index.record("darkmatte", [asset(100, "Dune", 1984)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"1984"}, pages={100: None})

    result = service(env, [FakeLibrary("Movies", "movie", [dune])], [], ["darkmatte"], tpdb).run()

    assert result.counts == {} and dune.uploads == []


def test_an_unreadable_page_for_a_title_only_match_does_not_hold_the_slot_up(env):
    dune = FakeItem(1, "Dune", 2021, 438631, path="/m/Dune (2021)/Dune.mkv")
    env.index.record("darkmatte", [asset(100, "Dune", 1984)])
    env.index.record("other", [asset(200, "Dune", 2021)])
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"2021"}, pages={100: "unreachable"})

    service(env, [FakeLibrary("Movies", "movie", [dune])], [], ["darkmatte", "other"], tpdb).run()

    assert dune.uploads == [b"2021"]


def test_titles_not_in_the_library_are_ignored_without_any_request(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    env.index.record("darkmatte", [asset(100, "Some Film I Don't Own", 2001)])
    tpdb = FakeTPDb()

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb).run()

    assert result.counts == {} and tpdb.page_requests == 0 and tpdb.image_requests == 0


def test_artwork_types_follow_the_filters(env):
    show = FakeItem(10, "Severance", 2022, 95396, location="/tv/Severance (2022)", seasons=(1,))
    env.index.record("darkmatte", [asset(100, "Severance", 2022, "show_cover"),
                                   asset(101, "Severance", 2022, "season_cover", season=1)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"cover", f"{TPDB}/101": b"s1"})

    service(env, [], [FakeLibrary("TV", "show", [show])], ["darkmatte"], tpdb,
            file_types=["show_cover"]).run()

    assert show.uploads == [b"cover"] and show.season_obj(1).uploads == []


def test_single_title_run_only_touches_that_title(env):
    alien = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/a.mkv")
    heat = FakeItem(2, "Heat", 1995, 949, path="/m/Heat (1995)/h.mkv")
    env.index.record("darkmatte", [asset(100, "Alien", 1979), asset(101, "Heat", 1995)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"alien", f"{TPDB}/101": b"heat"})

    service(env, [FakeLibrary("Movies", "movie", [alien, heat])], [], ["darkmatte"], tpdb).run(
        only_title=(MediaType.MOVIE.value, "Heat", 1995))

    assert heat.uploads == [b"heat"] and alien.uploads == []


def test_a_failed_upload_is_not_recorded_so_it_retries_next_run(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")

    def boom(filepath=None, url=None):
        raise RuntimeError("Plex said no")

    movie.uploadPoster = boom
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"img"})

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb,
                     retry_attempts=1).run()

    assert result.failed == 1
    assert env.store.all_applied() == {}
    assert env.callbacks.failed_counter[0] == 1


def test_folder_shared_by_different_items_is_left_alone(env):
    # Two different films side by side in one folder: Kometa would give both the same poster.
    a = FakeItem(1, "Alien", 1979, 348, path="/m/Sci-Fi/Alien.mkv")
    b = FakeItem(2, "Aliens", 1986, 679, path="/m/Sci-Fi/Aliens.mkv")
    folder = os.path.join(env.kometa, "Movies", "Sci-Fi")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"whatever-is-there")
    env.index.record("darkmatte", [asset(100, "Alien", 1979), asset(101, "Aliens", 1986)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"alien", f"{TPDB}/101": b"aliens"})

    result = service(env, [FakeLibrary("Movies", "movie", [a, b])], [], ["darkmatte"], tpdb).run()

    assert result.counts == {ACTION_SKIP: 2}
    assert read(os.path.join(folder, "poster.jpg")) == b"whatever-is-there"
    assert a.uploads == [] and b.uploads == []
    assert env.store.all_applied() == {}
    assert not os.path.exists(env.backup)


def test_editions_of_one_film_in_one_folder_are_not_treated_as_shared(env):
    cut = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    dc = FakeItem(2, "Alien", 1979, 348, path="/m/Alien (1979)/Alien {edition-Director's Cut}.mkv")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"alien"})

    result = service(env, [FakeLibrary("Movies", "movie", [cut, dc])], [], ["darkmatte"], tpdb).run()

    assert ACTION_SKIP not in result.counts
    assert read(os.path.join(env.kometa, "Movies", "Alien (1979)", "poster.jpg")) == b"alien"


def test_unreadable_poster_page_does_not_fall_back_to_a_lower_ranked_user(env):
    movie = FakeItem(1, "The Thing", 1982, 1091, path="/m/The Thing (1982)/x.mkv")
    env.index.record("darkmatte", [asset(100, "The Thing", 1983)])     # year one out: page needed
    env.index.record("other", [asset(200, "The Thing", 1982)])
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"lower"}, pages={100: "unreachable"})

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte", "other"], tpdb).run()

    assert result.counts == {}
    assert movie.uploads == []
    assert 100 not in env.store.all_meta()  # the failure isn't cached as 'no TMDb ID'


def test_a_backup_never_overwrites_an_earlier_backup(env):
    movie = FakeItem(1, "Heat", 1995, 949, path="/m/Heat (1995)/Heat.mkv")
    movies = FakeLibrary("Movies", "movie", [movie])
    folder = os.path.join(env.kometa, "Movies", "Heat (1995)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"original")
    env.index.record("other", [asset(200, "Heat", 1995)])
    tpdb = FakeTPDb(images={f"{TPDB}/200": b"second", f"{TPDB}/300": b"third"})
    svc = service(env, [movies], [], ["darkmatte", "other"], tpdb)
    svc.run()
    env.index.record("darkmatte", [asset(300, "Heat", 1995)])
    svc.run()  # same run stamp, so the same backup folder

    backup_dir = os.path.join(env.backup, svc.run_stamp, "Movies", "Heat (1995)")
    assert sorted(read(os.path.join(backup_dir, name)) for name in os.listdir(backup_dir)) == [b"original", b"second"]
    assert read(os.path.join(folder, "poster.jpg")) == b"third"


def test_a_winner_that_could_not_be_compared_is_kept_if_it_turns_out_identical(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    folder = os.path.join(env.kometa, "Movies", "Alien (1979)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"same")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"same"})
    tpdb.fail_once = {f"{TPDB}/100"}   # the comparison download fails, the apply download works

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb).run()

    assert result.counts == {ACTION_KEEP: 1}
    assert movie.uploads == []
    assert not os.path.exists(env.backup)
    assert os.listdir(folder) == ["poster.jpg"]
    assert env.store.all_applied()[("Movies", 1, "poster")]["asset_id"] == 100


def test_dry_run_reports_slots_it_could_not_compare(env):
    movie = FakeItem(1, "Alien", 1979, 348, path="/m/Alien (1979)/Alien.mkv")
    folder = os.path.join(env.kometa, "Movies", "Alien (1979)")
    os.makedirs(folder)
    with open(os.path.join(folder, "poster.jpg"), "wb") as f:
        f.write(b"whatever")
    env.index.record("darkmatte", [asset(100, "Alien", 1979)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"new"})
    tpdb.fail_once = {f"{TPDB}/100"}

    result = service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte"], tpdb, dry_run=True).run()

    assert result.counts == {ACTION_REPLACE: 1} and result.not_compared == 1
    assert any("couldn't be compared" in line for line in env.logs)


class _Response:
    def __init__(self, status, headers=None, content=b"img"):
        self.status_code = status
        self.headers = {"Content-Type": "image/jpeg", **(headers or {})}
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _client(monkeypatch, statuses, **kwargs):
    from services import ranked_service
    replies = iter(statuses)
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        status = next(replies)
        return status if isinstance(status, _Response) else _Response(status)
    monkeypatch.setattr(ranked_service.requests, "get", fake_get)
    waits = []
    messages = []
    client = ranked_service.TPDbClient(delay=0, attempts=1, sleep=waits.append, on_wait=messages.append, **kwargs)
    return client, calls, waits, messages


def test_too_many_requests_waits_and_tries_again(monkeypatch):
    client, calls, waits, messages = _client(monkeypatch, [429, 429, 200])

    data, ext = client.download("https://theposterdb.com/api/assets/1")

    assert data == b"img" and ext == ".jpg"
    assert len(calls) == 3
    assert [w for w in waits if w >= 1] == [60, 120]
    assert len(messages) == 2 and client.consecutive_failures == 0


def test_too_many_requests_honours_retry_after(monkeypatch):
    client, calls, waits, _ = _client(monkeypatch, [_Response(429, {"Retry-After": "45"}), 200])

    client.download("https://theposterdb.com/api/assets/1")

    assert [w for w in waits if w >= 1] == [45]


def test_downloads_stop_for_the_run_after_repeated_failures(monkeypatch):
    import requests
    client, calls, _, messages = _client(monkeypatch, [404] * 50)

    for _ in range(client.MAX_CONSECUTIVE_FAILURES):
        with pytest.raises(requests.exceptions.HTTPError):
            client.download("https://theposterdb.com/api/assets/1")
    assert client.gave_up and any("no more images" in m for m in messages)

    made = len(calls)
    with pytest.raises(Exception):
        client.download("https://theposterdb.com/api/assets/2")
    assert len(calls) == made   # nothing more is asked of ThePosterDB this run


def test_a_user_listed_twice_keeps_their_higher_place(env):
    movie = FakeItem(1, "Heat", 1995, 949, path="/m/Heat (1995)/Heat.mkv")
    env.index.record("darkmatte", [asset(100, "Heat", 1995)])
    env.index.record("other", [asset(200, "Heat", 1995)])
    tpdb = FakeTPDb(images={f"{TPDB}/100": b"darkmatte", f"{TPDB}/200": b"other"})

    service(env, [FakeLibrary("Movies", "movie", [movie])], [], ["darkmatte", "other", "DarkMatte"], tpdb).run()

    assert movie.uploads == [b"darkmatte"]
