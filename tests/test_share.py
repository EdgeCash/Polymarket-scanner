from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

from scanner.card import build_if_due
from scanner.config import load_settings
from scanner.share import HEIGHT, WIDTH, card_svg, pick_svg, to_png
from tests.test_card import seeded_diary


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def graded_card():
    from dataclasses import replace

    settings = load_settings(GAMELOG_SPORTS="nfl,cfb")
    diary, record, first, _ = seeded_diary()
    assert build_if_due(diary, settings, datetime(2026, 10, 11, 14, 5, tzinfo=UTC))["picks"] == 2
    kickoff = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)
    diary.store_football_game(replace(record, game_id=first.game_id), kickoff + timedelta(hours=4))
    diary.grade_card_picks(kickoff + timedelta(hours=6))
    return diary, settings


def test_the_card_image_lists_every_pick_with_its_result_and_the_record():
    diary, settings = graded_card()
    svg = card_svg(diary.card("2026-10-11"), diary.card_summary(), settings)
    assert svg.startswith("<svg") and f"viewBox='0 0 {WIDTH} {HEIGHT}'" in svg
    assert ">2026-10-11<" in svg and ">Sun 10:05 AM ET<" in svg
    assert ">Record 1-0-0, +0.95 units over 1 days<" in svg
    assert ">#1<" in svg and ">#2<" in svg and ">NFL<" in svg
    assert ">PIT at CLE · Sun 1:00 PM ET<" in svg and ">Over 41.5  (-105)<" in svg
    assert ">WSH +3.5  (-110)<" in svg and ">WIN +0.95u<" in svg and ">sim " in svg
    assert "Chosen by rule at 10:00 AM ET" in svg and "<image" not in svg
    png = to_png(svg)
    assert png_size(png) == (WIDTH * 2, HEIGHT * 2)
    assert png_size(to_png(svg, scale=1)) == (WIDTH, HEIGHT)


def test_the_pick_image_carries_the_call_the_numbers_and_the_histogram():
    diary, settings = graded_card()
    svg = pick_svg(diary.card_pick("2026-10-11", 1), settings)
    assert ">PIT at CLE<" in svg and ">Over 41.5<" in svg and ">(-105)<" in svg
    assert ">WIN +0.95u<" in svg and ">Final PIT 24, CLE 27<" in svg
    assert ">Model 51.0, book 41.5<" in svg and "0.90 of a game&#x27;s spread<" in svg
    assert "of 100,000 simulated games<" in svg and ">The price says 51%<" in svg
    assert "<svg x='520' y='130'" in svg and "book 41.5" in svg and "model 51.0" in svg
    assert "width='100%'" not in svg
    open_pick = pick_svg(diary.card_pick("2026-10-11", 2), settings)
    assert ">WSH +3.5<" in open_pick and "WIN" not in open_pick and "Final" not in open_pick
    assert "book WSH +3.5" in open_pick and "Margin (WSH, points)" in open_pick
    assert png_size(to_png(svg)) == (WIDTH * 2, HEIGHT * 2)


def test_the_player_image_lists_each_line_and_draws_the_first():
    from scanner.share import player_svg
    from tests.test_web import props_client

    client, diary, _ = props_client()
    lines = diary.player_projections("nba", "up-1")
    top = max((r for r in lines if r["stat"] == "points"), key=lambda r: r["mean"])
    mine = [r for r in lines if r["player_id"] == top["player_id"]]
    settings = load_settings(GAMELOG_SPORTS="nba")
    svg = player_svg("nba", diary.football_upcoming_game("nba", "up-1"), mine, settings)
    assert ">Jayson Tatum<" in svg and ">NBA<" in svg and ">F<" in svg
    assert ">Points<" in svg and f">{top['p10']}–{top['p90']}<" in svg
    assert "<svg x='560'" in svg and f"median {top['median']}" in svg  # points drawn first
    assert png_size(to_png(svg)) == (WIDTH * 2, HEIGHT * 2)
