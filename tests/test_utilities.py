from datetime import date, datetime, time

from pytest import raises

from pybluecurrent.utilities import parse_datetime_keys, parse_list_datetime_keys, parse_number_keys, to_jsonable


class TestToJsonable:
    def test_renders_dates_and_times(self):
        source = {"a": datetime(2026, 7, 3, 10, 30), "b": date(2026, 7, 3), "c": time(9, 30)}
        # A time of day renders as "HH:MM", the format the setters accept, so it can go straight back.
        assert to_jsonable(source) == {"a": "2026-07-03T10:30:00", "b": "2026-07-03", "c": "09:30"}

    def test_recurses_into_dicts_and_lists(self):
        source = {"delayed_charging": {"value": True, "start_time": time(23, 0), "days": [1, 2]}}
        assert to_jsonable(source) == {"delayed_charging": {"value": True, "start_time": "23:00", "days": [1, 2]}}

    def test_leaves_the_rest_alone(self):
        source = {"a": 1, "b": None, "c": "text", "d": [{"e": 2.5}]}
        result = to_jsonable(source)
        assert result == source
        assert result is not source and result["d"] is not source["d"]  # a copy, not the original


class TestParseDateTimeKeys:
    def test_format(self):
        source = {
            "a": "20230724 15:25:33",
            "b": "2023-06-27",
        }
        formats = {"a": ("%Y%m%d %H:%M:%S", False), "b": ("%Y-%m-%d", True)}
        result = parse_datetime_keys(source, formats=formats)
        assert result == {"a": datetime(2023, 7, 24, 15, 25, 33), "b": date(2023, 6, 27)}

    def test_missing_in_source(self):
        source = {}
        result = parse_datetime_keys(source, formats={"a": ("%Y-%m-%d", True)})
        assert source == result

    def test_passthrough(self):
        source = {"b": "2023-06-27"}
        result = parse_datetime_keys(source, formats={})
        assert source == result

    def test_empty_value(self):
        source = {"a": "", "b": ""}
        result = parse_datetime_keys(source, formats={"a": ("%Y-%m-%d", True)})
        assert result == {"a": None, "b": ""}

    def test_multiple_formats(self):
        # first_login_app was returned as "01-JAN-20" and is now returned as ISO "2020-01-15T13:33:52".
        formats = {"a": (("%d-%b-%y", "%Y-%m-%dT%H:%M:%S"), False)}
        assert parse_datetime_keys({"a": "01-JAN-20"}, formats) == {"a": datetime(2020, 1, 1)}
        assert parse_datetime_keys({"a": "2020-01-15T13:33:52"}, formats) == {"a": datetime(2020, 1, 15, 13, 33, 52)}

    def test_no_matching_format(self):
        with raises(ValueError):
            parse_datetime_keys({"a": "not a date"}, formats={"a": (("%d-%b-%y", "%Y-%m-%dT%H:%M:%S"), True)})


class TestParseListDateTimeKeys:
    def test_parse(self):
        formats = {"a": ("%Y%m%d %H:%M:%S", False), "b": ("%Y-%m-%d", True)}
        source = [{"a": ""}, {"b": "2023-06-27"}, {"a": "20230724 15:25:33"}]
        result = parse_list_datetime_keys(source, formats)
        assert result == [
            {"a": None},
            {"b": date(2023, 6, 27)},
            {"a": datetime(2023, 7, 24, 15, 25, 33)},
        ]


class TestParseNumberKeys:
    def test_strings(self):
        source = {"a": "1", "b": "   5.97", "c": "x"}
        result = parse_number_keys(source, types={"a": int, "b": float})
        assert result == {"a": 1, "b": 5.97, "c": "x"}
        assert isinstance(result["a"], int)

    def test_numbers_and_none_unchanged(self):
        source = {"a": 1, "b": 4.93, "c": None}
        assert parse_number_keys(source, types={"a": int, "b": float, "c": float}) == {"a": 1, "b": 4.93, "c": None}

    def test_missing_in_source(self):
        assert parse_number_keys({}, types={"a": int}) == {}

    def test_blank_string(self):
        assert parse_number_keys({"a": "  "}, types={"a": float}) == {"a": None}

    def test_not_a_number(self):
        with raises(ValueError):
            parse_number_keys({"a": "5,97"}, types={"a": float})
