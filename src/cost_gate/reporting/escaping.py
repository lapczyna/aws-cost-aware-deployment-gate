"""Making attacker-influenced text safe to put in a pull-request comment.

Logical IDs, tag values, intrinsic expressions and policy descriptions all originate in
files that anyone who can open a pull request controls, and all of them end up rendered
into Markdown that GitHub will display. Every one of them goes through this module.

What is being defended against, concretely:

* **HTML injection.** GitHub renders raw HTML in comments. A logical ID of
  ``<img src=x onerror=alert(1)>`` must not become live markup.
* **Table breaking.** An unescaped ``|`` ends a table cell early and corrupts every
  column after it, which turns a report into nonsense without anyone noticing.
* **Code-fence escape.** Content rendered inside backticks can close its own span and
  start arbitrary markup.
* **Fake mentions.** ``@everyone`` in a tag value should not notify anyone.
* **Control characters.** A carriage return or a zero-width character can hide content
  from a reviewer while leaving it in the artifact.
* **Unbounded length.** A comment has a hard size limit, so a hostile template must not
  be able to fill it.

The functions here are deliberately aggressive: over-escaping makes a report slightly
uglier, while under-escaping makes it a vector. Where the two conflict, ugly wins.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "DEFAULT_LIMIT",
    "code",
    "escape_markdown",
    "strip_control_characters",
    "table_cell",
    "truncate",
]

DEFAULT_LIMIT: Final = 120
"""Default cap for a single escaped value."""

_MARKDOWN_SPECIALS: Final = "\\`*_{}[]()#+-.!|~"
"""Characters with meaning in GitHub-flavoured Markdown, escaped with a backslash."""

_HTML_ENTITIES: Final = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
"""Characters that open markup, converted to entities rather than backslash-escaped.

A backslash-escaped ``<`` renders as a literal ``<`` only if the renderer honours the
escape. An entity is unambiguous in every Markdown implementation and in raw HTML, and
this is a security control rather than a formatting preference. ``&`` is converted
first, or the entities the others produce would themselves be mangled.
"""

_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH: Final = re.compile(
    # Written as escapes on purpose: a module that strips bidirectional overrides
    # must not carry invisible ones in its own source, where nobody can see them.
    "[\u200b-\u200f\u2028-\u2029\u202a-\u202e\u2060-\u2064\ufeff]"
)
"""Zero-width and bidirectional-override characters.

These render as nothing but change how the surrounding text reads — a bidirectional
override can make a resource name display in an order that does not match its bytes.
"""


def strip_control_characters(text: str) -> str:
    """Remove control, zero-width and bidirectional-override characters.

    Newlines and tabs survive as spaces: they are legitimate in a description, but a
    literal newline inside a table cell breaks the table.
    """
    cleaned = _CONTROL.sub("", text)
    cleaned = _ZERO_WIDTH.sub("", cleaned)
    return cleaned.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")


def truncate(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Shorten text, marking that it was shortened.

    The marker matters: a reader must be able to tell a truncated value from a short
    one, or they will believe they have seen the whole thing.
    """
    if limit <= 1 or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def escape_markdown(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Escape text for inclusion in Markdown prose.

    Markdown-significant characters are backslash-escaped, characters that open markup
    become HTML entities, and ``@`` is broken with an empty HTML comment so a mention
    cannot form.
    """
    cleaned = strip_control_characters(text)
    for character, entity in _HTML_ENTITIES:
        cleaned = cleaned.replace(character, entity)
    escaped = "".join(
        f"\\{character}" if character in _MARKDOWN_SPECIALS else character for character in cleaned
    )
    # GitHub's own idiom for defusing a mention. Renders invisibly.
    escaped = escaped.replace("@", "@<!---->")
    return truncate(escaped, limit)


def table_cell(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Escape text for a table cell.

    Pipes are escaped as HTML entities rather than backslashes: inside a table, a
    backslash-escaped pipe is rendered literally by some parsers and still splits the
    cell in others, whereas the entity is unambiguous everywhere.
    """
    return escape_markdown(text, limit).replace("\\|", "&#124;")


def code(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Render text as an inline code span that it cannot escape from.

    The fence is one backtick longer than the longest run inside the text, which is the
    mechanism Markdown itself defines for nesting backticks. A value consisting only of
    backticks would produce an empty span, so it falls back to escaped prose.

    **Not safe inside a table cell.** A code span preserves its content literally, so a
    ``|`` inside one still ends the cell. Use :func:`table_cell` there; this function is
    for prose and list items.
    """
    cleaned = truncate(strip_control_characters(text), limit)
    if not cleaned.strip():
        return escape_markdown(text, limit)

    longest = max((len(run) for run in re.findall(r"`+", cleaned)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if cleaned.startswith("`") or cleaned.endswith("`") else ""
    return f"{fence}{padding}{cleaned}{padding}{fence}"
