from datetime import date, datetime

from pybluecurrent.utilities import parse_datetime_keys, parse_list_datetime_keys


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
