from datetime import date

from pytest import mark, raises

from pybluecurrent import Weekday


class TestWeekday:
    def test_numbers_match_isoweekday(self):
        assert Weekday(date(2026, 7, 13).isoweekday()) == Weekday.MONDAY
        assert Weekday.SUNDAY == 7

    @mark.parametrize("value", ["mo", "MO", "Mon", "monday", "MONDAY", " monday "])
    def test_from_name(self, value: str):
        assert Weekday(value) == Weekday.MONDAY

    @mark.parametrize("value", ["tu", "th", "sa", "su"])
    def test_two_letters_are_unambiguous(self, value: str):
        assert Weekday(value).name.startswith(value.upper())

    @mark.parametrize("value", ["s", "t", "", "someday", 0, 8])
    def test_invalid(self, value: str | int):
        with raises(ValueError):
            Weekday(value)
