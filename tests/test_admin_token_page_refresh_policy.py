from pathlib import Path


TOKEN_JS = Path(__file__).resolve().parents[1] / "app/static/token/token.js"


def _read_token_js() -> str:
    return TOKEN_JS.read_text(encoding="utf-8-sig")


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    raise AssertionError(f"function {name} body was not closed")


def test_token_page_does_not_poll_tokens_api():
    source = _read_token_js()

    assert "setInterval" not in source
    assert "startLiveStats" not in source
    assert "refreshStatsOnly" not in source


def test_filter_changes_reload_tokens_from_server():
    source = _read_token_js()

    assert "loadData(" in _function_body(source, "onFilterChange")
    assert "loadData(" in _function_body(source, "resetFilters")
