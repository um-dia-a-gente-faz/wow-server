#!/usr/bin/env python3
"""The standard chat lines (#139): a fixed, committed list an agent may say.

Why a closed list: a model once put a bare `}` into public chat (UM-92), and a
brain that cannot generate text (Jev, docs/adr/0001) still has to say
something sane. Anything that wants to post a "standard" line - the capability
probe (agent/tools/probe.py), the fleet panel's "say something" button - picks
an id from here and gets the text from here; it never supplies the text.

`resolve_line()` is the gate: it accepts a line id, or text that is *exactly*
one of the committed lines, and raises LineError for everything else. Free text
is rejected, not sanitised, so this cannot become a way to post arbitrary text
through the fleet panel. (Arbitrary free-text arguments to chat actions are a
separate problem, validated where they enter the action layer.)

Rules for editing the list, enforced by check_catalog() in the unit tests:
short, plain ASCII (letters, digits, space and . , ! ? ' - only), no markup,
unique ids and unique wording.
"""

# (id, category, text). Order is stable: the panel shows it as written.
_LINES = (
    ("greet_hello",   "greeting", "Hello there!"),
    ("greet_hi",      "greeting", "Hi, everyone."),
    ("greet_morning", "greeting", "Good to see you all."),
    ("still_here",    "status",   "Still here."),
    ("status_busy",   "status",   "Busy at the moment, back in a bit."),
    ("status_lost",   "status",   "I think I took a wrong turn somewhere."),
    ("help_nearby",   "help",     "Could someone nearby give me a hand?"),
    ("help_danger",   "help",     "Careful, there is danger around here!"),
    ("thanks",        "thanks",   "Thanks, that helped a lot."),
    ("thanks_party",  "thanks",   "Thank you for the company."),
    ("sorry",         "apology",  "Sorry about that!"),
    ("sorry_late",    "apology",  "Sorry, I was a bit slow there."),
    ("farewell",      "farewell", "See you around."),
)

# The line the capability probe posts: short, harmless, recognisable.
PROBE_LINE_ID = "still_here"

MAX_LINE_LEN = 80  # well under the 255-byte wire limit (actions._encode_message)
_ALLOWED_PUNCT = set(" .,!?'-")


class LineError(ValueError):
    """The requested text is not one of the standard lines."""


LINES: dict[str, str] = {line_id: text for line_id, _cat, text in _LINES}
CATEGORIES: dict[str, str] = {line_id: cat for line_id, cat, _text in _LINES}
_BY_TEXT: dict[str, str] = {text: line_id for line_id, text in LINES.items()}


def standard_lines() -> list[dict]:
    """The list as JSON-ready dicts (id/category/text), in display order -
    what a panel offers as its choices."""
    return [{"id": line_id, "category": cat, "text": text} for line_id, cat, text in _LINES]


def line_error(value) -> str | None:
    """Why `value` is not a standard line, or None when it is (id or exact text)."""
    if not isinstance(value, str) or not value:
        return "chat line must be a non-empty string"
    if value in LINES or value in _BY_TEXT:
        return None
    return (f"{value!r} is not a standard chat line; pick one of: "
            + ", ".join(sorted(LINES)))


def resolve_line(value) -> str:
    """The committed text for a line id, or for text that is exactly a
    committed line. Raises LineError for anything else - there is no fuzzy
    matching, so what reaches the wire is always one of the literals above."""
    error = line_error(value)
    if error is not None:
        raise LineError(error)
    return LINES[value] if value in LINES else value


def check_catalog() -> list[str]:
    """Problems with the list itself (empty = fine). Run from the unit tests so a
    careless edit to _LINES fails CI instead of reaching public chat."""
    problems = []
    seen_ids, seen_text = set(), set()
    for line_id, cat, text in _LINES:
        if line_id in seen_ids:
            problems.append(f"duplicate id {line_id!r}")
        seen_ids.add(line_id)
        norm = " ".join(text.lower().split())
        if norm in seen_text:
            problems.append(f"duplicate wording {text!r}")
        seen_text.add(norm)
        if not cat:
            problems.append(f"{line_id}: empty category")
        if not text.strip() or text != text.strip():
            problems.append(f"{line_id}: empty or padded text")
        if len(text) > MAX_LINE_LEN:
            problems.append(f"{line_id}: longer than {MAX_LINE_LEN} characters")
        bad = sorted({c for c in text if not (c.isascii() and (c.isalnum() or c in _ALLOWED_PUNCT))})
        if bad:
            problems.append(f"{line_id}: disallowed characters {bad!r}")
        if "  " in text:
            problems.append(f"{line_id}: double space")
    if PROBE_LINE_ID not in LINES:
        problems.append(f"PROBE_LINE_ID {PROBE_LINE_ID!r} is not in the list")
    return problems
