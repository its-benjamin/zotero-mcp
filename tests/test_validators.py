"""Tests for shared input validators in tools/_helpers.py."""

from zotero_mcp.tools._helpers import (
    validate_collection_key,
    validate_item_key,
    validate_library_id,
    validate_tag,
)


class TestValidateItemKey:
    def test_valid_8char_alnum(self):
        assert validate_item_key("ABCD1234") is None

    def test_valid_lowercase(self):
        assert validate_item_key("abcd1234") is None

    def test_valid_with_whitespace_stripped(self):
        assert validate_item_key("  ABCD1234  ") is None

    def test_empty_string(self):
        assert "empty" in validate_item_key("").lower()

    def test_whitespace_only(self):
        assert "empty" in validate_item_key("   ").lower()

    def test_none(self):
        assert "empty" in validate_item_key(None).lower()

    def test_too_short(self):
        err = validate_item_key("ABC123")
        assert "format" in err.lower() and "8" in err

    def test_too_long(self):
        err = validate_item_key("ABCD12345")
        assert "format" in err.lower() and "8" in err

    def test_special_chars(self):
        assert validate_item_key("ABCD-123") is not None

    def test_with_dot(self):
        assert validate_item_key("ABCD.123") is not None


class TestValidateCollectionKey:
    def test_valid(self):
        assert validate_collection_key("COLL1234") is None

    def test_empty(self):
        assert validate_collection_key("") is not None

    def test_wrong_length(self):
        assert validate_collection_key("COLL") is not None

    def test_special_chars(self):
        assert validate_collection_key("COLL/234") is not None


class TestValidateLibraryId:
    def test_valid_int(self):
        assert validate_library_id(12345) is None

    def test_valid_str(self):
        assert validate_library_id("12345") is None

    def test_zero(self):
        err = validate_library_id(0)
        assert "positive" in err.lower()

    def test_negative(self):
        err = validate_library_id(-1)
        assert "positive" in err.lower()

    def test_non_numeric(self):
        assert validate_library_id("abc") is not None

    def test_none(self):
        assert "empty" in validate_library_id(None).lower()


class TestValidateTag:
    def test_valid(self):
        assert validate_tag("important") is None

    def test_with_spaces(self):
        assert validate_tag("to read") is None

    def test_unicode(self):
        assert validate_tag("研究") is None

    def test_empty(self):
        assert "empty" in validate_tag("").lower()

    def test_none(self):
        assert "empty" in validate_tag(None).lower()

    def test_too_long(self):
        err = validate_tag("x" * 256)
        assert "long" in err.lower()
