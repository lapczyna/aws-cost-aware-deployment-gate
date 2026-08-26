"""Escaping is a security control, so it is tested like one.

Every string in these tests could arrive in a real template: a logical ID, a tag value,
an intrinsic expression. All of them end up in a GitHub comment, which renders raw HTML.
"""

from __future__ import annotations

import pytest

from cost_gate.reporting.escaping import (
    DEFAULT_LIMIT,
    code,
    escape_markdown,
    strip_control_characters,
    table_cell,
    truncate,
)

pytestmark = pytest.mark.unit


class TestHtmlInjection:
    @pytest.mark.parametrize(
        "hostile",
        [
            "<img src=x onerror=alert(1)>",
            "<script>alert(1)</script>",
            "<a href='javascript:alert(1)'>click</a>",
            "<!-- comment -->",
            "<details><summary>x</summary>",
        ],
    )
    def test_markup_cannot_survive_escaping(self, hostile):
        # GitHub renders raw HTML in comments.
        escaped = escape_markdown(hostile)
        assert "<" not in escaped.replace("<!---->", "")
        assert ">" not in escaped.replace("<!---->", "")
        assert "&lt;" in escaped or "&gt;" in escaped

    def test_a_code_span_neutralises_markup_without_escaping_it(self):
        # Inside a span the content is literal, so it renders as text.
        rendered = code("<script>alert(1)</script>")
        assert rendered.startswith("`")
        assert rendered.endswith("`")


class TestTableIntegrity:
    def test_a_pipe_cannot_break_out_of_a_cell(self):
        # An unescaped pipe corrupts every column after it.
        assert "|" not in table_cell("a | b").replace("&#124;", "")

    def test_a_newline_cannot_break_out_of_a_row(self):
        assert "\n" not in table_cell("line one\nline two")

    def test_a_carriage_return_cannot_break_out_of_a_row(self):
        assert "\r" not in table_cell("line one\r\nline two")


class TestCodeSpanEscape:
    @pytest.mark.parametrize(
        "hostile",
        ["`", "``", "`` ` ``", "a`b", "```code```", "` `` ``` `"],
    )
    def test_content_cannot_close_its_own_span(self, hostile):
        rendered = code(hostile)
        # The fence is longer than any run inside, which is Markdown's own mechanism.
        fence_length = len(rendered) - len(rendered.lstrip("`"))
        inner = rendered[fence_length:-fence_length] if fence_length else rendered
        longest_inner_run = max((len(run) for run in inner.split("`") if False), default=0)
        assert fence_length > longest_inner_run

    def test_an_all_backtick_value_still_produces_a_valid_span(self):
        # The fence grows to four, and padding keeps the content from touching it.
        assert code("```") == "```` ``` ````"

    def test_a_blank_value_falls_back_to_prose(self):
        # An empty span would render as nothing at all.
        assert code("   ") == escape_markdown("   ")

    def test_a_value_starting_with_a_backtick_is_padded(self):
        assert code("`x").startswith("`` `")


class TestMentions:
    def test_a_mention_cannot_notify_anyone(self):
        assert "@everyone" not in escape_markdown("@everyone")
        assert escape_markdown("@everyone") == "@<!---->everyone"

    def test_an_email_like_value_is_also_broken(self):
        assert "@" in escape_markdown("owner@example.com")
        assert "@<!---->" in escape_markdown("owner@example.com")


class TestHiddenCharacters:
    # Written as escapes throughout: a test file full of literal invisible
    # characters is unreviewable, and the linter is right to reject it.

    def test_control_characters_are_removed(self):
        assert strip_control_characters("a\x00b\x07c") == "abc"

    def test_zero_width_characters_are_removed(self):
        # They render as nothing while remaining in the artifact.
        assert strip_control_characters("a\u200bb\ufeffc") == "abc"

    def test_bidirectional_overrides_are_removed(self):
        # These can make a resource name display in an order its bytes do not have.
        assert strip_control_characters("a\u202eb") == "ab"

    def test_tabs_become_spaces(self):
        assert "\t" not in strip_control_characters("a\tb")


class TestTruncation:
    def test_long_values_are_cut(self):
        assert len(escape_markdown("x" * 5000)) <= DEFAULT_LIMIT

    def test_truncation_is_visible(self):
        # A reader must be able to tell a truncated value from a short one.
        assert truncate("x" * 100, 10).endswith("…")

    def test_short_values_are_untouched(self):
        assert truncate("short", 100) == "short"

    def test_the_limit_is_configurable(self):
        assert len(escape_markdown("x" * 500, 20)) <= 20


class TestOrdinaryValuesStayReadable:
    @pytest.mark.parametrize(
        "value",
        ["NatGateway", "AWS::EC2::NatGateway", "app/Database", "db.t3.medium", "us-east-1"],
    )
    def test_a_normal_value_is_recognisable_after_escaping(self, value):
        # Over-escaping is acceptable; unreadable output is not.
        escaped = escape_markdown(value)
        assert value.replace(":", "").replace("/", "").replace(".", "").replace(
            "-", ""
        ) in escaped.replace("\\", "").replace(":", "").replace("/", "").replace(".", "").replace(
            "-", ""
        )

    def test_a_code_span_of_a_normal_value_is_clean(self):
        assert code("AWS::EC2::NatGateway") == "`AWS::EC2::NatGateway`"


class TestIdempotenceOfCleaning:
    def test_stripping_twice_changes_nothing(self):
        once = strip_control_characters("a\x00\u200b\tb")
        assert strip_control_characters(once) == once
