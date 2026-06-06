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


def test_filter_changes_render_locally_without_reload():
    source = _read_token_js()

    assert "applyLocalView(" in _function_body(source, "onFilterChange")
    assert "applyLocalView(" in _function_body(source, "resetFilters")
    assert "loadData(" not in _function_body(source, "onFilterChange")
    assert "loadData(" not in _function_body(source, "resetFilters")


def test_import_uses_incremental_endpoint_and_indexed_dedupe():
    source = _read_token_js()
    body = _function_body(source, "submitImport")

    assert "new Set(tokenIndex.keys())" in body
    assert "addTokensToServer(" in body
    assert "syncToServer(" not in body
    assert "flatTokens.some" not in body


def test_batch_refresh_has_no_client_side_chunk_delay():
    source = _read_token_js()
    body = _function_body(source, "processBatchQueue")

    assert "setTimeout(" not in body
    assert "batchQueue.splice(0, batchQueue.length)" in body
