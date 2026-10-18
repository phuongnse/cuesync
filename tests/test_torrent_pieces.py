"""Tests for the torrent piece math."""

from __future__ import annotations

import pytest

from cuesync.torrent import index_ranges, pieces_for_range

PL = 100


def test_single_byte_in_the_first_piece():
    assert list(pieces_for_range(0, PL, 0, 1)) == [0]


def test_range_ending_on_a_piece_edge():
    assert list(pieces_for_range(0, PL, 0, 100)) == [0]
    assert list(pieces_for_range(0, PL, 0, 101)) == [0, 1]
    assert list(pieces_for_range(0, PL, 100, 200)) == [1]


def test_range_starting_on_a_piece_edge():
    assert list(pieces_for_range(0, PL, 100, 101)) == [1]
    assert list(pieces_for_range(0, PL, 99, 101)) == [0, 1]


def test_file_starting_mid_piece():
    assert list(pieces_for_range(250, PL, 0, 1)) == [2]
    assert list(pieces_for_range(250, PL, 0, 50)) == [2]
    assert list(pieces_for_range(250, PL, 0, 51)) == [2, 3]
    assert list(pieces_for_range(250, PL, 50, 150)) == [3]
    assert list(pieces_for_range(250, PL, 50, 151)) == [3, 4]


def test_last_byte_of_the_file():
    size = 1000
    assert list(pieces_for_range(250, PL, size - 1, size)) == [12]
    assert list(pieces_for_range(0, PL, size - 1, size)) == [9]


@pytest.mark.parametrize(("start", "end"), [(5, 5), (7, 3)])
def test_empty_range(start: int, end: int):
    assert pieces_for_range(0, PL, start, end) == range(0)


def test_piece_length_must_be_positive():
    with pytest.raises(ValueError, match="piece_length"):
        pieces_for_range(0, 0, 0, 1)


def test_index_ranges_of_a_small_file_is_one_range():
    assert index_ranges(1000, head=600, tail=600) == [(0, 1000)]
    assert index_ranges(1000, head=500, tail=500) == [(0, 1000)]
    assert index_ranges(10, head=600, tail=600) == [(0, 10)]


def test_index_ranges_of_a_large_file_is_head_and_tail():
    assert index_ranges(10_000, head=600, tail=700) == [(0, 600), (9300, 10_000)]


def test_index_ranges_of_an_empty_file():
    assert index_ranges(0) == []
