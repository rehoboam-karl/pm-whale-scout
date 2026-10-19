"""Tests for pm-whale-scout."""

from whale_scout import _percentile, recommend_changes


class TestPercentile:
    def test_empty(self):
        assert _percentile(10.0, []) == 0.0

    def test_single_value(self):
        assert _percentile(5.0, [5.0]) == 100.0

    def test_normalizes_to_max(self):
        assert _percentile(50.0, [0.0, 50.0, 100.0]) == 50.0

    def test_caps_at_100(self):
        assert _percentile(200.0, [50.0]) == 100.0

    def test_zero_max(self):
        assert _percentile(10.0, [0.0, 0.0]) == 0.0


class TestRecommendChanges:
    def test_empty_current(self):
        scored = [
            {"address": "aaa", "name": "Alice", "composite_score": 50.0},
            {"address": "bbb", "name": "Bob", "composite_score": 30.0},
        ]
        to_add, to_remove = recommend_changes(scored, [])
        assert len(to_add) == 2
        assert len(to_remove) == 0

    def test_removes_low_score(self):
        scored = [
            {"address": "aaa", "name": "Alice", "composite_score": 50.0},
            {"address": "bbb", "name": "Bob", "composite_score": 3.0},
        ]
        _to_add, to_remove = recommend_changes(scored, ["aaa", "bbb"])
        assert len(to_remove) == 1
        assert to_remove[0]["address"] == "bbb"

    def test_does_not_add_current_whales(self):
        scored = [
            {"address": "aaa", "name": "Alice", "composite_score": 50.0},
        ]
        to_add, _to_remove = recommend_changes(scored, ["aaa"])
        assert len(to_add) == 0
