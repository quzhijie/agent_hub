from app.textutil import clean_frame, meaningful_tail, strip_ansi


def test_strip_ansi_removes_color_and_cursor():
    raw = "\x1b[31mred\x1b[0m\x1b[2J\x1b[Hplain"
    assert strip_ansi(raw) == "redplain"


def test_strip_ansi_collapses_carriage_returns():
    raw = "loading 10%\rloading 100%"
    assert strip_ansi(raw) == "loading 100%"


def test_clean_frame_trims_blank_edges():
    # trailing whitespace and blank edge lines go; leading indentation is kept
    raw = "\n\n  hello  \n\n\n"
    assert clean_frame(raw) == "  hello"


def test_meaningful_tail_skips_box_chrome_and_empty_prompt():
    frame = "\n".join([
        "assistant: done, two files changed.",
        "╭────────────────────╮",
        "│ >                  │",
        "╰────────────────────╯",
        "",
    ])
    tail = meaningful_tail(frame)
    assert "done, two files changed" in tail
    assert "╭" not in tail and "│ >" not in tail
