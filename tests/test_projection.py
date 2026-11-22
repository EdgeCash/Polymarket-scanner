from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from scanner.books import BookLine, fair
from scanner.gamelog import GameRecord, TeamRef
from scanner.projection import (
    BOOK_PSEUDO_GAMES,
    MODEL_VERSION,
    TUNING,
    fit,
    grade,
    home_win_chance,
    project,
    summarise,
    uncertainty,
    win_probability,
)

TEAMS = {t: TeamRef(t, t * 3, f"Team {t}") for t in "ABCD"}
STRENGTH = {"A": 7.0, "B": 3.0, "C": -3.0, "D": -7.0}  # true margin against an average side
START = datetime(2026, 9, 6, tzinfo=UTC)


def record(game_id, home, away, home_score, away_score, neutral=False, yards=True, day=0):
    def stats(score):
        out = {"points": score, "first_half_points": score // 2}
        if yards:
            out["total_yards"] = score * 15
        return out

    return GameRecord(
        "nfl",
        game_id,
        2026,
        1 + day // 7,
        START + timedelta(days=day),
        neutral,
        TEAMS[home],
        TEAMS[away],
        home_score,
        away_score,
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        stats(home_score),
        stats(away_score),
    )


def season(rounds=3, noise=0.0, seed=1, yards=True):
    """A round-robin where each side scores its strength over an average 23, home +0.9."""
    rng = random.Random(seed)
    out = []
    for _ in range(rounds):
        for home in "ABCD":
            for away in "ABCD":
                if home == away:
                    continue
                hs = 23 + STRENGTH[home] / 2 + 0.9 + rng.gauss(0, noise)
                as_ = 23 + STRENGTH[away] / 2 - 0.9 + rng.gauss(0, noise)
                out.append(
                    record(
                        str(len(out)), home, away, round(hs), round(as_), day=len(out), yards=yards
                    )
                )
    return out


def test_fit_recovers_the_pecking_order_and_shrinks_toward_average():
    ratings = fit(season(), "nfl")
    assert ratings.sport == "nfl" and ratings.season == 2026 and ratings.games_fitted == 36
    assert ratings.games == {"A": 18, "B": 18, "C": 18, "D": 18}
    off = ratings.offense
    assert off["A"] > off["B"] > 0 > off["C"] > off["D"]
    # The true offence edge is 3.5; shrinkage with 18 games keeps most of it.
    assert 2.0 < off["A"] < 3.5 and -3.5 < off["D"] < -2.0
    assert all(abs(d) < 1.0 for d in ratings.defense.values())  # every defence was average
    assert ratings.league_points == pytest.approx(23.0, abs=0.5)
    assert ratings.points_per_volume == pytest.approx(1 / 15, abs=0.001)
    assert 0.44 < ratings.first_half_share < 0.5
    assert (ratings.hfa, ratings.sigma) == (TUNING["nfl"].hfa, TUNING["nfl"].sigma)
    assert ratings.team("A") == {"offense": off["A"], "defense": ratings.defense["A"], "games": 18}
    assert ratings.team("Z") is None


def test_fit_copes_without_yards_or_without_games():
    empty = fit([], "cfb")
    assert empty.games == {} and empty.league_points == 0.0 and empty.points_per_volume is None
    assert empty.first_half_share == pytest.approx(0.48) and empty.hfa == TUNING["cfb"].hfa
    no_yards = fit(season(yards=False), "nfl")
    assert no_yards.points_per_volume is None
    assert no_yards.offense["A"] > no_yards.offense["B"] > no_yards.offense["C"]


def test_project_adds_home_field_and_blends_with_the_book():
    ratings = fit(season(), "nfl")
    book = BookLine("DraftKings", -300, 240, None, 46.5, -110, -110, -7.5, -110, -110)
    p = project(ratings, "A", "D", False, book)
    raw, blend = p["raw"], p["blend"]
    assert p["model"] == MODEL_VERSION and p["games_used"] == {"home": 18, "away": 18}
    assert not p["thin"] and not p["neutral"]
    assert raw["home"] > raw["away"] and raw["margin"] == pytest.approx(raw["home"] - raw["away"])
    assert raw["total"] == pytest.approx(raw["home"] + raw["away"])
    assert 5.0 < raw["margin"] < 10.0 and 0.6 < raw["home_win"] < 0.8
    assert raw["first_half"]["home"] == pytest.approx(raw["home"] * ratings.first_half_share)
    neutral = project(ratings, "A", "D", True, None)
    assert neutral["raw"]["margin"] == pytest.approx(raw["margin"] - ratings.hfa)
    assert neutral["neutral"] and neutral["book"] is None and neutral["gap"] is None
    assert neutral["blend"]["margin"] == pytest.approx(neutral["raw"]["margin"])  # nothing to blend
    # The blend weighs the model by how far it can be trusted against the book: the
    # book's error variance over the two together (inverse-variance weighting), so the
    # NFL's four-game ratings carry a few percent, and the blend's spread is narrower.
    unc = uncertainty("nfl", 18, 18)
    weight = unc["weight"]
    assert p["uncertainty"] == unc and 0.0 < weight < 0.1
    assert blend["weight"] == pytest.approx(weight)
    assert blend["margin"] == pytest.approx(weight * raw["margin"] + (1 - weight) * 7.5)
    assert blend["total"] == pytest.approx(weight * raw["total"] + (1 - weight) * 46.5)
    assert blend["home"] - blend["away"] == pytest.approx(blend["margin"])
    assert (
        p["league"]["sigma"] == pytest.approx(unc["sigma"]) and p["league"]["sigma_sport"] == 13.5
    )
    assert blend["sigma"] == pytest.approx(unc["sigma_blend"])
    assert blend["sigma"] < p["league"]["sigma"]
    assert 0.6 < blend["home_win"] < 0.8
    assert p["book"] == {
        "margin": 7.5,
        "total": 46.5,
        "home_win": pytest.approx(fair(-300, 240)[0]),
    }
    assert p["gap"]["margin"] == pytest.approx(raw["margin"] - 7.5)
    assert p["gap"]["total"] == pytest.approx(raw["total"] - 46.5)
    assert p["gap"]["home_win"] == pytest.approx(raw["home_win"] - fair(-300, 240)[0])
    assert p["ratings"]["home"]["offense"] == ratings.offense["A"]
    assert p["league"]["points"] == ratings.league_points and p["league"]["games"] == 36
    assert project(ratings, "A", "Z", False, book) is None
    spread_only = BookLine("DraftKings", None, None, None, None, None, None, -7.5, -110, -110)
    q = project(ratings, "A", "D", False, spread_only)
    assert q["blend"]["total"] == pytest.approx(q["raw"]["total"]) and q["gap"]["total"] is None
    # The blend's win chance follows its own margin through the grid, moneyline or not.
    assert q["book"]["home_win"] is None
    b = q["blend"]
    assert b["home_win"] == pytest.approx(
        home_win_chance("nfl", b["margin"], b["total"], b["sigma"], b["sigma_total"])
    )


def test_a_projection_from_one_game_is_thin_and_leans_on_the_book():
    ratings = fit([record("g", "A", "B", 24, 20)], "nfl")
    p = project(ratings, "A", "B", False, None)
    assert p["thin"] and p["games_used"] == {"home": 1, "away": 1}
    # One game leaves the ratings less sure than eighteen: a wider spread, less weight.
    settled = project(fit(season(), "nfl"), "A", "B", False, None)
    assert p["blend"]["weight"] < settled["blend"]["weight"]
    assert p["league"]["sigma"] > settled["league"]["sigma"] > 13.5
    # Without a book there is nothing to blend: the blend is the model at its own spread.
    assert (
        p["blend"]["margin"] == p["raw"]["margin"] and p["blend"]["sigma"] == p["league"]["sigma"]
    )


def test_a_sport_without_uncertainty_numbers_keeps_the_fixed_weight_blend(monkeypatch):
    plain = TUNING["nfl"].__class__(hfa=1.8, sigma=13.5, sigma_total=13.5)
    monkeypatch.setitem(TUNING, "plain", plain)
    assert plain.book_sigma is None and uncertainty("plain", 3, 3) is None
    assert BOOK_PSEUDO_GAMES == 6.0


def test_fit_shrinks_toward_last_seasons_rating_when_a_prior_is_given():
    priors = {
        "season": 2025,
        "teams": {
            "A": {"offense": 6.0, "defense": -6.0},
            "B": {"offense": 2.0, "defense": -2.0},
            "C": {"offense": -2.0, "defense": 2.0},
            "D": {"offense": -6.0, "defense": 6.0},
        },
        "unknown": {"offense": -5.0, "defense": 5.0},
    }
    r = TUNING["nfl"].prior_weight
    # No games yet: every team sits at its share of last season, and a game can be
    # projected (thin) from the priors alone.
    empty = fit([], "nfl", priors)
    assert empty.prior_season == 2025 and empty.prior_teams == 4
    assert empty.offense["A"] == pytest.approx(r * 6.0) and empty.games["A"] == 0
    p = project(empty, "A", "D", False, None)
    assert p is not None and p["thin"] and p["raw"]["margin"] > 0
    # With one game, the rating moves from the prior toward what the game said, not
    # toward average; a side the prior does not know starts at the default.
    one = fit([record("g", "D", "A", 30, 10)], "nfl", priors)
    assert one.offense["D"] > empty.offense["D"] and one.offense["A"] < empty.offense["A"]
    stranger = fit([record("g", "A", "B", 24, 20)], "nfl", {**priors, "teams": {}})
    assert stranger.prior_teams == 0 and stranger.defense["A"] > 0  # from the default
    # And without a prior the ratings start at zero, as before.
    assert fit([], "nfl").prior_season is None


def test_win_probability_is_a_normal_cdf_of_the_margin():
    assert win_probability(0.0, 13.5) == 0.5
    assert win_probability(13.5, 13.5) == pytest.approx(0.8413, abs=1e-3)
    assert win_probability(-13.5, 13.5) == pytest.approx(1 - win_probability(13.5, 13.5))


def projection(**changes):
    base = {
        "raw": {"margin": 6.0, "total": 48.0, "home_win": 0.65},
        "blend": {"margin": 5.0, "total": 47.0, "home_win": 0.6},
        "book": {"margin": 3.0, "total": 45.0, "home_win": 0.55},
    }
    base.update(changes)
    return base


def test_grade_scores_the_model_against_the_final_and_the_book():
    close = BookLine("DraftKings", -200, 170, None, 46.0, -110, -110, -4.0, -110, -110)
    g = grade(projection(), 27, 17, close)  # margin 10, total 44
    assert g["final"] == {"home": 27, "away": 17}
    assert g["margin_error"] == {"raw": 4.0, "blend": 5.0, "book": 7.0}
    assert g["total_error"] == {"raw": 4.0, "blend": 3.0, "book": 1.0}
    assert g["brier"]["raw"] == pytest.approx(0.35**2) and g["brier"]["book"] == pytest.approx(
        0.45**2
    )
    assert g["ats"] == {"side": "home", "gap": 3.0, "result": "win"}  # 10 beat the 3 the book had
    assert g["ou"] == {"side": "over", "gap": 3.0, "result": "loss"}  # 44 stayed under 45
    assert g["clv"] == {"margin": 1.0, "total": 1.0}  # the line moved to 4 and 46, our way

    away_side = grade(
        projection(raw={"margin": 1.0, "total": 40.0, "home_win": 0.5}), 20, 24, close
    )
    assert away_side["ats"] == {"side": "away", "gap": 2.0, "result": "win"}
    assert away_side["ou"] == {"side": "under", "gap": 5.0, "result": "win"}
    assert away_side["clv"] == {"margin": -1.0, "total": -1.0}
    assert away_side["brier"]["raw"] == pytest.approx(0.25)

    push = grade(projection(), 24, 21, None)  # margin exactly the book's 3
    assert push["ats"]["result"] == "push" and push["clv"] is None
    tie = grade(projection(), 20, 20, None)
    assert tie["brier"]["raw"] == pytest.approx((0.65 - 0.5) ** 2)

    no_book = grade(projection(book=None), 27, 17, close)
    assert no_book["ats"] is None and no_book["ou"] is None
    assert no_book["margin_error"]["book"] is None and no_book["brier"]["book"] is None
    assert no_book["clv"] == {"margin": None, "total": None}
    same_as_book = grade(
        projection(raw={"margin": 3.0, "total": 45.0, "home_win": 0.55}), 27, 17, None
    )
    assert same_as_book["ats"] is None and same_as_book["ou"] is None


def test_summarise_buckets_by_how_far_the_model_sat_from_the_book():
    def graded(gap_margin, gap_total, win_ats, win_ou, clv=1.0):
        return {
            "final": {"home": 1, "away": 0},
            "margin_error": {"raw": 4.0, "blend": 5.0, "book": None},
            "total_error": {"raw": 2.0, "blend": 2.0, "book": 3.0},
            "brier": {"raw": 0.2, "blend": 0.25, "book": 0.3},
            "ats": {"side": "home", "gap": gap_margin, "result": "win" if win_ats else "loss"},
            "ou": {"side": "over", "gap": gap_total, "result": "win" if win_ou else "push"},
            "clv": {"margin": clv, "total": None},
        }

    grades = [
        graded(0.5, 1.5, True, True),
        graded(2.5, 2.5, False, False, clv=-0.5),
        graded(6.0, 0.2, True, True, clv=0.0),
    ]
    out = summarise(grades)
    assert out["graded"] == 3
    assert out["margin_error"] == {"raw": 4.0, "blend": 5.0, "book": None}
    assert out["brier"]["book"] == pytest.approx(0.3)
    assert out["ats"]["1"] == {"wins": 1, "losses": 1, "pushes": 0}
    assert out["ats"]["2"] == {"wins": 1, "losses": 1, "pushes": 0}
    assert out["ats"]["3"] == {"wins": 1, "losses": 0, "pushes": 0}
    assert out["ats"]["5"] == {"wins": 1, "losses": 0, "pushes": 0}
    assert out["ou"]["1"] == {"wins": 1, "losses": 0, "pushes": 1}
    assert out["ou"]["5"] == {"wins": 0, "losses": 0, "pushes": 0}
    # The share counts the lines that moved: one of the two, not one of three.
    assert out["clv"] == {
        "margin": pytest.approx(0.5 / 3),
        "total": None,
        "closed": 3,
        "moved": 2,
        "positive_share": pytest.approx(0.5),
    }
    empty = summarise([])
    assert empty["graded"] == 0 and empty["margin_error"]["raw"] is None
    assert empty["clv"]["positive_share"] is None and empty["ats"]["1"]["wins"] == 0


def test_hockey_reads_the_books_margin_off_its_moneyline():

    from scanner.grid import grid
    from scanner.projection import book_margin, tuning_for

    book = BookLine("DraftKings", -230, 190, None, 6.5, 105, -125, -1.5, 110, -130)
    margin = book_margin(book, "nhl")
    p_home = fair(-230, 190)[0]
    # Read off the grid's own win curve, so the model and the book compare on one scale.
    tuning = tuning_for("nhl")
    assert grid("nhl", margin, 6.5, tuning.sigma, tuning.sigma_total).home_win == pytest.approx(
        p_home, abs=1e-3
    )
    assert 0.5 < margin < 2.0  # a -230 favourite by under two goals, not the puck line
    assert book_margin(book, "nfl") == 1.5  # football keeps the spread
    assert book_margin(None, "nhl") is None
    no_moneyline = BookLine("DraftKings", None, None, None, 6.5, 105, -125, -1.5, 110, -130)
    assert book_margin(no_moneyline, "nhl") is None


def test_hockey_fits_on_shots_as_the_volume_half_with_finer_gap_buckets():
    from scanner.projection import book_margin

    def rec(game_id, home, away, hs, as_, shots_home, shots_away, day):
        return GameRecord(
            "nhl",
            game_id,
            2027,
            None,
            START + timedelta(days=day),
            False,
            TEAMS[home],
            TEAMS[away],
            hs,
            as_,
            (),
            (),
            {"points": hs, "first_half_points": 1, "shots": shots_home},
            {"points": as_, "first_half_points": 0, "shots": shots_away},
        )

    records = [
        rec("1", "A", "B", 4, 1, 35, 20, 0),
        rec("2", "B", "A", 2, 3, 25, 30, 1),
        rec("3", "A", "C", 5, 2, 40, 22, 2),
        rec("4", "C", "B", 2, 2, 30, 30, 3),
    ]
    ratings = fit(records, "nhl")
    assert (ratings.hfa, ratings.sigma, ratings.sigma_total) == (0.2, 2.55, 2.3)
    assert ratings.points_per_volume == pytest.approx(21 / 232)
    assert ratings.first_half_share == pytest.approx(4 / 21)
    assert ratings.offense["A"] > ratings.offense["B"] and ratings.season == 2027
    book = BookLine("DraftKings", -150, 130, None, 6.5, -110, -110, -1.5, 110, -130)
    p = project(ratings, "A", "B", False, book)
    assert p["sport"] == "nhl" and p["league"]["points_per_volume"] == ratings.points_per_volume
    assert p["book"]["margin"] == pytest.approx(book_margin(book, "nhl"))
    close = BookLine("DraftKings", -200, 170, None, 6.5, -110, -110, -1.5, 110, -130)
    g = grade(p, 3, 2, close)
    assert g["ats"] is not None and g["clv"]["margin"] is not None
    s = summarise([g], "nhl")
    assert s["buckets"] == ["0.25", "0.5", "0.75", "1"] and set(s["ats"]) == set(s["buckets"])
    assert summarise([], "nfl")["buckets"] == ["1", "2", "3", "5"] == summarise([])["buckets"]
    assert fit([], "nba").first_half_share == 0.5 and fit([], "nba").hfa == 2.0


def test_college_basketball_has_its_own_numbers_and_its_overtime():
    from scanner.projection import TUNING, tuning_for
    from scanner.simulate import simulate

    college = tuning_for("cbb")
    assert college is TUNING["cbb"] and college.hfa == 4.0 and college.sigma == 12.0
    assert college.sigma_total == 17.0 and college.volume_stat is None
    assert not college.margin_from_moneyline and college.buckets == TUNING["nba"].buckets
    sim = simulate(
        {"raw": {"margin": 2.0, "total": 146.0, "home_win": 0.57}, "league": {}},
        "cbb",
        draws=10_000,
        seed=5,
    )
    # A level game plays five more minutes; the mean keeps the projection's, which is
    # fit to final scores, overtime included.
    assert sim["total"]["mean"] == pytest.approx(146.0, abs=1e-3)
    # Level after regulation as often as close college games are (6.2% across all).
    assert 0.04 < sim["overtime"] < 0.09 and "0" not in sim["margin"]["hist"]


def test_the_rating_dials_are_off_until_a_fit_earns_them_and_do_what_they_say(monkeypatch):
    from dataclasses import replace

    from scanner.projection import luck_free_scores

    assert all(
        t.fumble_points == 0 and t.interception_points == 0 and t.margin_cap is None
        for t in TUNING.values()
    )  # fmt: skip
    # Fading old games earned its place in basketball, pro and college.
    assert {sport for sport, t in TUNING.items() if t.half_life} == {"nba", "cbb"}
    assert all(t.three_luck in (0.0, 0.25) for t in TUNING.values())
    # Three-point luck: a side that made 6 more threes than the league's rate on its
    # attempts gives back the share the tuning calls luck, at three points each.
    hot = record("h", "A", "D", 120, 100, day=98)
    hot.home_stats.update(three_made=18, three_att=30)
    hot.away_stats.update(three_made=12, three_att=30)
    lucky_share = replace(TUNING["nfl"], three_luck=0.5)
    scores = luck_free_scores(hot, lucky_share, league_three_rate=0.4)
    assert scores == {"home": pytest.approx(120 - 0.5 * 3 * 6), "away": 100.0}
    assert luck_free_scores(hot, lucky_share) == {"home": 120.0, "away": 100.0}  # no rate yet
    games = season()
    plain = fit(games, "nfl")
    # Turnover luck: a side that took the ball away twice gives half of that back.
    lucky = record("x", "A", "D", 30, 10, day=99)
    lucky.home_stats.update(fumbles_lost=0, interceptions=0)
    lucky.away_stats.update(fumbles_lost=2, interceptions=1)
    assert luck_free_scores(lucky, TUNING["nfl"]) == {"home": 30.0, "away": 10.0}
    priced = replace(TUNING["nfl"], fumble_points=4.0, interception_points=2.0)
    assert luck_free_scores(lucky, priced) == {"home": 25.0, "away": 15.0}  # 10 points, halved
    no_stats = record("y", "A", "D", 30, 10, day=99)
    assert luck_free_scores(no_stats, priced) == {"home": 30.0, "away": 10.0}
    # A cap on the margin: a 60-point rout counts as the cap and no more.
    rout = [*games, record("z", "A", "D", 70, 10, day=100)]
    monkeypatch.setitem(TUNING, "nfl", replace(TUNING["nfl"], margin_cap=21.0))
    capped = fit(rout, "nfl")
    monkeypatch.setitem(TUNING, "nfl", replace(TUNING["nfl"], margin_cap=None))
    uncapped = fit(rout, "nfl")
    assert capped.offense["A"] < uncapped.offense["A"]
    assert capped.offense["A"] - capped.defense["A"] > plain.offense["A"] - plain.defense["A"] - 1
    # Fading: with a short half-life the latest game outweighs the old ones.
    monkeypatch.setitem(TUNING, "nfl", replace(TUNING["nfl"], half_life=2.0))
    faded = fit(rout, "nfl")
    assert faded.offense["A"] > uncapped.offense["A"]
    assert faded.games["A"] == uncapped.games["A"]  # the count of games is still the count


def test_the_home_edge_can_differ_inside_a_conference_and_outside_one(monkeypatch):
    from dataclasses import replace

    from scanner.projection import home_edge

    plain = TUNING["cfb"]
    assert home_edge(plain, False, True) == home_edge(plain, False, None) == plain.hfa
    assert home_edge(plain, True, True) == 0.0
    split = replace(plain, hfa_conference=2.0, hfa_nonconference=6.0)
    assert home_edge(split, False, True) == 2.0 and home_edge(split, False, False) == 6.0
    assert home_edge(split, False, None) == plain.hfa and home_edge(split, True, False) == 0.0
    monkeypatch.setitem(TUNING, "cfb", split)
    games = [replace(g, sport="cfb", conference_game=i % 2 == 0) for i, g in enumerate(season())]
    ratings = fit(games, "cfb")
    inside = project(ratings, "A", "D", False, None, {"conference_game": True})
    outside = project(ratings, "A", "D", False, None, {"conference_game": False})
    unknown = project(ratings, "A", "D", False, None)
    assert outside["raw"]["margin"] - inside["raw"]["margin"] == pytest.approx(4.0)
    assert unknown["raw"]["margin"] - inside["raw"]["margin"] == pytest.approx(plain.hfa - 2.0)
    assert inside["league"]["hfa"] == 2.0 and outside["league"]["hfa"] == 6.0
    assert inside["raw"]["total"] == pytest.approx(outside["raw"]["total"])
    neutral = project(ratings, "A", "D", True, None, {"conference_game": False})
    assert neutral["raw"]["margin"] == pytest.approx(inside["raw"]["margin"] - 2.0)


def test_a_weighted_prior_is_taken_whole_and_a_plain_one_at_the_sports_share():
    from scanner.projection import prior_means

    teams = {"A": {"offense": 4.0, "defense": -2.0}}
    plain = {"season": 2025, "teams": teams, "unknown": {"offense": -10.0, "defense": 10.0}}
    by_team, default = prior_means(plain, TUNING["cbb"])
    share = TUNING["cbb"].prior_weight
    assert by_team["A"] == (pytest.approx(share * 4.0), pytest.approx(share * -2.0))
    assert default == (pytest.approx(share * -10.0), pytest.approx(share * 10.0))
    by_team, default = prior_means({**plain, "weighted": True}, TUNING["cbb"])
    assert by_team["A"] == (4.0, -2.0) and default == (-10.0, 10.0)
    assert prior_means(None, TUNING["cbb"]) == ({}, (0.0, 0.0))


def test_the_shipped_college_basketball_prior_is_this_seasons_roster_forecast():
    from scanner import priors

    priors.reload()
    entry = priors.priors_for("cbb", 2027)
    assert entry is not None and entry["weighted"] is True and len(entry["teams"]) > 300
    assert set(entry["roster"]["coefficients"]) >= {"rating_back", "share", "arrivals"}
    nets = [v["offense"] - v["defense"] for v in entry["teams"].values()]
    assert -60 < min(nets) < 0 < max(nets) < 40
    # The other sports keep last season's ratings at their usual weight.
    assert not priors.priors_for("nba", 2027).get("weighted")
