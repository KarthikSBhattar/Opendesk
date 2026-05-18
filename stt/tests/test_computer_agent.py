"""Unit tests for computer_agent.py — pure functions only, no I/O."""
import computer_agent as ca


# ── parse_action ─────────────────────────────────────────────────────────────

def test_parse_action_plain_json():
    result = ca.parse_action('{"action": "click", "box_2d": [100, 200, 150, 250]}')
    assert result == {"action": "click", "box_2d": [100, 200, 150, 250]}


def test_parse_action_markdown_fence():
    text = '```json\n{"action": "key", "key": "cmd+t"}\n```'
    result = ca.parse_action(text)
    assert result == {"action": "key", "key": "cmd+t"}


def test_parse_action_bare_markdown_fence():
    text = '```\n{"action": "wait", "seconds": 1}\n```'
    result = ca.parse_action(text)
    assert result == {"action": "wait", "seconds": 1}


def test_parse_action_embedded_in_text():
    text = 'Here is the action: {"action": "scroll", "direction": "down"} and that is it.'
    result = ca.parse_action(text)
    assert result == {"action": "scroll", "direction": "down"}


def test_parse_action_malformed_json():
    assert ca.parse_action('{"action": "click", "box_2d": [100,}') is None


def test_parse_action_no_braces():
    assert ca.parse_action("just some text") is None


def test_parse_action_empty_string():
    assert ca.parse_action("") is None


def test_parse_action_whitespace_only():
    assert ca.parse_action("   \n\t  ") is None


def test_parse_action_nested_object():
    text = '{"action": "done", "result": "finished", "meta": {"steps": 3}}'
    result = ca.parse_action(text)
    assert result is not None
    assert result["action"] == "done"
    assert result["meta"]["steps"] == 3


def test_parse_action_strips_leading_trailing_whitespace():
    result = ca.parse_action('  \n  {"action": "wait"}  \n  ')
    assert result == {"action": "wait"}


# ── _clip_unit ────────────────────────────────────────────────────────────────

def test_clip_unit_below_zero():
    assert ca._clip_unit(-50.0) == 0.0


def test_clip_unit_above_max():
    assert ca._clip_unit(1500.0) == 1000.0


def test_clip_unit_at_zero():
    assert ca._clip_unit(0.0) == 0.0


def test_clip_unit_at_max():
    assert ca._clip_unit(1000.0) == 1000.0


def test_clip_unit_normal():
    assert ca._clip_unit(500.0) == 500.0


# ── _normalized_box_center ────────────────────────────────────────────────────

def test_normalized_box_center_basic():
    # [y1, x1, y2, x2] → center_x, center_y
    result = ca._normalized_box_center([100, 200, 200, 400])
    assert result is not None
    cx, cy = result
    assert cx == 300.0  # (200+400)/2
    assert cy == 150.0  # (100+200)/2


def test_normalized_box_center_swapped_y():
    result = ca._normalized_box_center([200, 200, 100, 400])
    assert result is not None
    _, cy = result
    assert cy == 150.0


def test_normalized_box_center_swapped_x():
    result = ca._normalized_box_center([100, 400, 200, 200])
    assert result is not None
    cx, _ = result
    assert cx == 300.0


def test_normalized_box_center_clipping():
    # Values outside 0-1000 get clipped before centering
    result = ca._normalized_box_center([-100, 0, 2000, 1000])
    assert result is not None
    cx, cy = result
    assert cx == 500.0   # (0+1000)/2
    assert cy == 500.0   # (0+1000)/2


def test_normalized_box_center_not_a_list():
    assert ca._normalized_box_center("not a list") is None  # type: ignore[arg-type]


def test_normalized_box_center_wrong_length():
    assert ca._normalized_box_center([100, 200, 300]) is None


def test_normalized_box_center_empty():
    assert ca._normalized_box_center([]) is None


def test_normalized_box_center_non_numeric():
    assert ca._normalized_box_center(["a", "b", "c", "d"]) is None


def test_normalized_box_center_none():
    assert ca._normalized_box_center(None) is None  # type: ignore[arg-type]


# ── describe_action ───────────────────────────────────────────────────────────

def test_describe_action_uses_speak_when_present():
    action = {"action": "click", "speak": "Clicking the submit button"}
    assert ca.describe_action(action) == "Clicking the submit button"


def test_describe_action_click_with_label():
    action = {"action": "click", "label": "Submit"}
    assert ca.describe_action(action) == "Clicking Submit"


def test_describe_action_click_no_label():
    action = {"action": "click"}
    assert ca.describe_action(action) == "Clicking the target"


def test_describe_action_type_short():
    action = {"action": "type", "text": "hello world"}
    assert ca.describe_action(action) == "Typing hello world"


def test_describe_action_type_truncates_at_48():
    long_text = "a" * 60
    result = ca.describe_action({"action": "type", "text": long_text})
    assert len(result) <= len("Typing ") + 48


def test_describe_action_key():
    action = {"action": "key", "key": "cmd+t"}
    assert ca.describe_action(action) == "Pressing cmd+t"


def test_describe_action_open_url():
    action = {"action": "open_url", "url": "https://example.com"}
    assert ca.describe_action(action) == "Opening https://example.com"


def test_describe_action_open_app():
    action = {"action": "open_app", "app": "Safari"}
    assert ca.describe_action(action) == "Opening Safari"


def test_describe_action_scroll_down():
    action = {"action": "scroll", "direction": "down"}
    assert ca.describe_action(action) == "Scrolling down"


def test_describe_action_scroll_up():
    action = {"action": "scroll", "direction": "up"}
    assert ca.describe_action(action) == "Scrolling up"


def test_describe_action_wait():
    action = {"action": "wait"}
    assert ca.describe_action(action) == "Waiting for the screen to update"


def test_describe_action_unknown():
    action = {"action": "teleport"}
    assert ca.describe_action(action) == "teleport"


def test_describe_action_empty():
    assert ca.describe_action({}) == "Working"


# ── _scroll_clicks ────────────────────────────────────────────────────────────

def test_scroll_clicks_small():
    assert ca._scroll_clicks({"distance": "small", "amount": 1}) == 5


def test_scroll_clicks_medium():
    assert ca._scroll_clicks({"distance": "medium", "amount": 1}) == 10


def test_scroll_clicks_large():
    assert ca._scroll_clicks({"distance": "large", "amount": 1}) == 18


def test_scroll_clicks_page():
    assert ca._scroll_clicks({"distance": "page", "amount": 1}) == 28


def test_scroll_clicks_unknown_defaults_to_large():
    assert ca._scroll_clicks({"distance": "huge", "amount": 1}) == 18


def test_scroll_clicks_amount_multiplies():
    assert ca._scroll_clicks({"distance": "small", "amount": 3}) == 15


def test_scroll_clicks_amount_clamped_at_5():
    assert ca._scroll_clicks({"distance": "small", "amount": 99}) == 5 * 5


def test_scroll_clicks_amount_clamped_at_1():
    assert ca._scroll_clicks({"distance": "small", "amount": 0}) == 5 * 1


def test_scroll_clicks_invalid_amount_defaults_to_1():
    assert ca._scroll_clicks({"distance": "small", "amount": "lots"}) == 5


def test_scroll_clicks_no_keys():
    # Missing distance defaults to large (18), missing amount defaults to 1
    assert ca._scroll_clicks({}) == 18


# ── _normalize_keys ───────────────────────────────────────────────────────────

def test_normalize_keys_cmd():
    assert ca._normalize_keys("cmd") == ["command"]


def test_normalize_keys_alt():
    assert ca._normalize_keys("alt") == ["option"]


def test_normalize_keys_opt():
    assert ca._normalize_keys("opt") == ["option"]


def test_normalize_keys_ctrl_passthrough():
    assert ca._normalize_keys("ctrl") == ["ctrl"]


def test_normalize_keys_combo():
    result = ca._normalize_keys("cmd+shift+t")
    assert result == ["command", "shift", "t"]


def test_normalize_keys_alt_combo():
    result = ca._normalize_keys("alt+f4")
    assert result == ["option", "f4"]


def test_normalize_keys_uppercase_lowercased():
    result = ca._normalize_keys("CMD+T")
    assert result == ["command", "t"]


# ── _env_int ──────────────────────────────────────────────────────────────────

def test_env_int_default_when_missing(monkeypatch):
    monkeypatch.delenv("TEST_ENV_INT_MISSING", raising=False)
    assert ca._env_int("TEST_ENV_INT_MISSING", 42, 0, 100) == 42


def test_env_int_clamps_minimum(monkeypatch):
    monkeypatch.setenv("TEST_ENV_INT_MIN", "5")
    assert ca._env_int("TEST_ENV_INT_MIN", 50, 10, 100) == 10


def test_env_int_clamps_maximum(monkeypatch):
    monkeypatch.setenv("TEST_ENV_INT_MAX", "200")
    assert ca._env_int("TEST_ENV_INT_MAX", 50, 0, 100) == 100


def test_env_int_valid_value(monkeypatch):
    monkeypatch.setenv("TEST_ENV_INT_OK", "25")
    assert ca._env_int("TEST_ENV_INT_OK", 50, 0, 100) == 25


def test_env_int_invalid_returns_default(monkeypatch):
    monkeypatch.setenv("TEST_ENV_INT_BAD", "notanumber")
    assert ca._env_int("TEST_ENV_INT_BAD", 42, 0, 100) == 42
