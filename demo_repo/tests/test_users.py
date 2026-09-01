"""Regression test that intentionally fails until DEMO-3 is implemented."""

from app.users import audit_display_name, profile_display_name


def test_both_paths_use_the_profile_display_name_format() -> None:
    expected = "Ada Lovelace"
    assert profile_display_name(" Ada ", " Lovelace ") == expected
    assert audit_display_name(" Ada ", " Lovelace ") == expected
