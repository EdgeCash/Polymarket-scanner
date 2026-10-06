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
    project,
    summarise,
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
    assert ratings.points_per_yard == pytest.approx(1 / 15, abs=0.001)
    assert 0.44 < ratings.first_half_share < 0.5
    assert (ratings.hfa, ratings.sigma) == (TUNING["nfl"].hfa, TUNING["nfl"].sigma)
    assert ratings.team("A") == {"offense": off["A"], "defense": ratings.defense["A"], "games": 18}
    assert ratings.team("Z") is None


def test_fit_copes_without_yards_or_without_games():
    empty = fit([], "cfb")
    assert empty.games == {} and empty.league_points == 0.0 and empty.points_per_yard is None
    assert empty.first_half_share == pytest.approx(0.48) and empty.hfa == TUNING["cfb"].hfa
    no_yards = fit(season(yards=False), "nfl")
    assert no_yards.points_per_yard is None
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
    weight = 18 / (18 + BOOK_PSEUDO_GAMES)
    assert blend["weight"] == pytest.approx(weight)
    assert blend["margin"] == pytest.approx(weight * raw["margin"] + (1 - weight) * 7.5)
    assert blend["total"] == pytest.approx(weight * raw["total"] + (1 - weight) * 46.5)
    assert blend["home"] - blend["away"] == pytest.approx(blend["margin"])
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
    assert q["book"]["home_win"] is None and q["blend"]["home_win"] == q["raw"]["home_win"]


def test_a_projection_from_one_game_is_thin_and_leans_on_the_book():
    ratings = fit([record("g", "A", "B", 24, 20)], "nfl")
    p = project(ratings, "A", "B", False, None)
    assert p["thin"] and p["games_used"] == {"home": 1, "away": 1}
    assert p["blend"]["weight"] == pytest.approx(1 / (1 + BOOK_PSEUDO_GAMES))


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
    assert out["clv"] == {
        "margin": pytest.approx(0.5 / 3),
        "total": None,
        "closed": 3,
        "positive_share": pytest.approx(1 / 3),
    }
    empty = summarise([])
    assert empty["graded"] == 0 and empty["margin_error"]["raw"] is None
    assert empty["clv"]["positive_share"] is None and empty["ats"]["1"]["wins"] == 0
