"""Unit tests for realtime_stt_bridge.py — pure utility functions only."""
import queue

import pytest

# Import the module; heavy deps (pyaudio, RealtimeSTT, google-genai) are
# loaded lazily inside functions so this import is safe without a display/mic.
import realtime_stt_bridge as bridge

# ── _action_signature ─────────────────────────────────────────────────────────

def test_action_signature_click_with_box():
    action = {"action": "click", "box_2d": [100, 200, 150, 250], "label": "Submit"}
    sig = bridge._action_signature(action)
    assert sig.startswith("click:")
    assert "submit" in sig


def test_action_signature_click_rounds_to_25():
    # Coordinates get rounded to nearest 25; two close-but-different boxes
    # that round to the same bucket should produce the same signature.
    a1 = {"action": "click", "box_2d": [101, 201, 149, 249], "label": "btn"}
    a2 = {"action": "click", "box_2d": [112, 212, 138, 238], "label": "btn"}
    assert bridge._action_signature(a1) == bridge._action_signature(a2)


def test_action_signature_click_different_positions():
    a1 = {"action": "click", "box_2d": [0, 0, 25, 25], "label": "a"}
    a2 = {"action": "click", "box_2d": [500, 500, 600, 600], "label": "a"}
    assert bridge._action_signature(a1) != bridge._action_signature(a2)


def test_action_signature_key():
    sig = bridge._action_signature({"action": "key", "key": "cmd+t"})
    assert sig == "key:cmd+t"


def test_action_signature_key_case_normalized():
    sig = bridge._action_signature({"action": "key", "key": "CMD+T"})
    assert sig == "key:cmd+t"


def test_action_signature_open_app():
    sig = bridge._action_signature({"action": "open_app", "app": "Safari"})
    assert sig == "open_app:safari"


def test_action_signature_open_url():
    sig = bridge._action_signature({"action": "open_url", "url": "https://google.com"})
    assert sig == "open_url:https://google.com"


def test_action_signature_scroll_down():
    sig = bridge._action_signature({"action": "scroll", "direction": "down"})
    assert sig == "scroll:down"


def test_action_signature_scroll_up():
    sig = bridge._action_signature({"action": "scroll", "direction": "up"})
    assert sig == "scroll:up"


def test_action_signature_wait_always_same():
    s1 = bridge._action_signature({"action": "wait", "seconds": 1})
    s2 = bridge._action_signature({"action": "wait", "seconds": 2})
    assert s1 == s2 == "wait"


def test_action_signature_type():
    sig = bridge._action_signature({"action": "type", "text": "hello"})
    assert sig.startswith("type:hello")


def test_action_signature_type_truncates_at_80():
    long_text = "x" * 100
    sig = bridge._action_signature({"action": "type", "text": long_text})
    # The text portion should be capped at 80 chars
    assert sig.count("x") <= 80


def test_action_signature_unknown_action():
    sig = bridge._action_signature({"action": "teleport"})
    assert sig == "teleport"


# ── _is_repeated_stall ────────────────────────────────────────────────────────

def test_stall_empty_recent():
    assert bridge._is_repeated_stall("click", "click:100:200:150:250:btn", []) is False


def test_stall_empty_signature():
    assert bridge._is_repeated_stall("click", "", ["click:100:200:150:250:btn"]) is False


def test_stall_wait_consecutive():
    assert bridge._is_repeated_stall("wait", "wait", ["wait"]) is True


def test_stall_wait_not_last():
    assert bridge._is_repeated_stall("wait", "wait", ["scroll:down", "wait"]) is True


def test_stall_wait_not_consecutive():
    assert bridge._is_repeated_stall("wait", "wait", ["scroll:down"]) is False


def test_stall_click_twice_is_stall():
    sig = "click:100:200:150:250:btn"
    assert bridge._is_repeated_stall("click", sig, [sig, "scroll:down", sig]) is True


def test_stall_click_once_not_stall():
    sig = "click:100:200:150:250:btn"
    assert bridge._is_repeated_stall("click", sig, ["scroll:down", sig]) is False


def test_stall_key_in_last_two():
    sig = "key:cmd+t"
    assert bridge._is_repeated_stall("key", sig, ["scroll:down", sig]) is True


def test_stall_key_not_in_last_two():
    sig = "key:cmd+t"
    assert bridge._is_repeated_stall("key", sig, [sig, "scroll:down", "scroll:down"]) is False


def test_stall_open_app_in_last_two():
    sig = "open_app:safari"
    assert bridge._is_repeated_stall("open_app", sig, ["scroll:down", sig]) is True


def test_stall_scroll_three_in_a_row():
    sig = "scroll:down"
    assert bridge._is_repeated_stall("scroll", sig, [sig, sig, sig]) is True


def test_stall_scroll_two_in_a_row_not_stall():
    sig = "scroll:down"
    assert bridge._is_repeated_stall("scroll", sig, ["scroll:up", sig, sig]) is False


def test_stall_scroll_mixed_not_stall():
    assert bridge._is_repeated_stall("scroll", "scroll:down", ["scroll:down", "scroll:up", "scroll:down"]) is False


def test_stall_scroll_fewer_than_three():
    sig = "scroll:down"
    assert bridge._is_repeated_stall("scroll", sig, [sig, sig]) is False


def test_stall_unknown_kind_never_stalls():
    assert bridge._is_repeated_stall("drag", "drag", ["drag", "drag", "drag"]) is False


# ── _promote_compound_direct_action ──────────────────────────────────────────

def test_promote_non_direct_action_passthrough():
    route = {"type": "computer_task", "goal": "do something"}
    result = bridge._promote_compound_direct_action("do something", route)
    assert result["type"] == "computer_task"


def test_promote_open_url_unchanged():
    route = {"type": "direct_action", "action": "open_url", "url": "https://google.com"}
    result = bridge._promote_compound_direct_action("open google.com", route)
    assert result["type"] == "direct_action"


def test_promote_simple_key_unchanged():
    route = {"type": "direct_action", "action": "key", "key": "cmd+space"}
    result = bridge._promote_compound_direct_action("spotlight search", route)
    assert result["type"] == "direct_action"


def test_promote_key_with_new_tab_and_search():
    route = {"type": "direct_action", "action": "key", "key": "cmd+t"}
    result = bridge._promote_compound_direct_action("open a new tab and search for cats", route)
    assert result["type"] == "computer_task"
    assert "goal" in result


def test_promote_open_app_then_do():
    route = {"type": "direct_action", "action": "open_app", "app": "Safari"}
    result = bridge._promote_compound_direct_action("open Safari and then search for dogs", route)
    assert result["type"] == "computer_task"


def test_promote_key_then_type():
    route = {"type": "direct_action", "action": "key", "key": "cmd+t"}
    result = bridge._promote_compound_direct_action("open new tab then type my query", route)
    assert result["type"] == "computer_task"


def test_promote_open_app_no_compound_unchanged():
    route = {"type": "direct_action", "action": "open_app", "app": "Calculator"}
    result = bridge._promote_compound_direct_action("open Calculator", route)
    assert result["type"] == "direct_action"


def test_promote_preserves_speak_in_promoted_route():
    route = {"type": "direct_action", "action": "key", "key": "cmd+t", "speak": "Opening tab"}
    result = bridge._promote_compound_direct_action("open a new tab and google something", route)
    assert result.get("speak") == "Opening tab"


# ── _select_input_device ──────────────────────────────────────────────────────

FAKE_DEVICES = [
    {"index": 0, "name": "BlackHole 2ch", "maxInputChannels": 2},
    {"index": 1, "name": "MacBook Pro Microphone", "maxInputChannels": 1},
    {"index": 2, "name": "External USB Mic", "maxInputChannels": 2},
]


def test_select_device_by_exact_index(monkeypatch):
    monkeypatch.setenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", "2")
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", raising=False)
    idx, name = bridge._select_input_device(FAKE_DEVICES)
    assert idx == 2
    assert name is not None and "USB Mic" in name


def test_select_device_by_index_not_found(monkeypatch):
    monkeypatch.setenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", "99")
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", raising=False)
    with pytest.raises(ValueError, match="not visible"):
        bridge._select_input_device(FAKE_DEVICES)


def test_select_device_by_invalid_index(monkeypatch):
    monkeypatch.setenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", "notanumber")
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", raising=False)
    with pytest.raises(ValueError):
        bridge._select_input_device(FAKE_DEVICES)


def test_select_device_by_name_match(monkeypatch):
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", raising=False)
    monkeypatch.setenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", "usb mic")
    idx, _name = bridge._select_input_device(FAKE_DEVICES)
    assert idx == 2


def test_select_device_by_name_not_found(monkeypatch):
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", raising=False)
    monkeypatch.setenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", "nonexistent mic")
    with pytest.raises(ValueError, match="No input device matches"):
        bridge._select_input_device(FAKE_DEVICES)


def test_select_device_prefers_physical_over_virtual(monkeypatch):
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", raising=False)
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", raising=False)
    idx, _name = bridge._select_input_device(FAKE_DEVICES)
    # BlackHole is virtual, should pick index 1 (MacBook Pro Microphone)
    assert idx == 1


def test_select_device_only_virtual_returns_none(monkeypatch):
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_INDEX", raising=False)
    monkeypatch.delenv("OPEN_DESK_STT_INPUT_DEVICE_MATCH", raising=False)
    virtual_only = [{"index": 0, "name": "BlackHole 2ch", "maxInputChannels": 2}]
    idx, name = bridge._select_input_device(virtual_only)
    assert idx is None
    assert name is None


# ── _is_virtual_input ─────────────────────────────────────────────────────────

def test_is_virtual_blackhole():
    assert bridge._is_virtual_input({"name": "BlackHole 2ch"}) is True


def test_is_virtual_soundflower():
    assert bridge._is_virtual_input({"name": "Soundflower (2ch)"}) is True


def test_is_virtual_loopback():
    assert bridge._is_virtual_input({"name": "Loopback Audio"}) is True


def test_is_virtual_real_microphone():
    assert bridge._is_virtual_input({"name": "MacBook Pro Microphone"}) is False


def test_is_virtual_external_mic():
    assert bridge._is_virtual_input({"name": "Blue Yeti USB Microphone"}) is False


def test_is_virtual_case_insensitive():
    assert bridge._is_virtual_input({"name": "BLACKHOLE 16CH"}) is True


# ── broadcast ────────────────────────────────────────────────────────────────

def test_broadcast_delivers_to_registered_client():
    q = queue.Queue()
    with bridge.CLIENTS_LOCK:
        bridge.CLIENTS.add(q)
    try:
        bridge.broadcast({"type": "listening"})
        payload = q.get_nowait()
        assert payload["type"] == "listening"
        assert "ts" in payload
    finally:
        with bridge.CLIENTS_LOCK:
            bridge.CLIENTS.discard(q)


def test_broadcast_sticky_event_updates_latest_status():
    bridge.broadcast({"type": "server_ready", "message": "up"})
    with bridge.LATEST_STATUS_LOCK:
        assert bridge.LATEST_STATUS is not None
        assert bridge.LATEST_STATUS["type"] == "server_ready"


def test_broadcast_non_sticky_does_not_update_latest_status():
    bridge.broadcast({"type": "server_ready"})
    bridge.broadcast({"type": "ai_token", "text": "hello"})

    with bridge.LATEST_STATUS_LOCK:
        current = bridge.LATEST_STATUS or {}
    # LATEST_STATUS should still reflect the last sticky event, not ai_token
    assert current.get("type") != "ai_token"


def test_broadcast_adds_timestamp_automatically():
    q = queue.Queue()
    with bridge.CLIENTS_LOCK:
        bridge.CLIENTS.add(q)
    try:
        bridge.broadcast({"type": "ai_done"})
        payload = q.get_nowait()
        assert isinstance(payload["ts"], float)
    finally:
        with bridge.CLIENTS_LOCK:
            bridge.CLIENTS.discard(q)


def test_broadcast_full_queue_does_not_raise():
    q = queue.Queue(maxsize=1)
    q.put({"type": "existing"})  # fill the queue
    with bridge.CLIENTS_LOCK:
        bridge.CLIENTS.add(q)
    try:
        bridge.broadcast({"type": "ai_done"})  # should silently skip
    finally:
        with bridge.CLIENTS_LOCK:
            bridge.CLIENTS.discard(q)


# ── _env_int (bridge copy) ────────────────────────────────────────────────────

def test_bridge_env_int_default(monkeypatch):
    monkeypatch.delenv("BRIDGE_TEST_INT", raising=False)
    assert bridge._env_int("BRIDGE_TEST_INT", 7, 1, 20) == 7


def test_bridge_env_int_clamped(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_INT", "999")
    assert bridge._env_int("BRIDGE_TEST_INT", 7, 1, 20) == 20


def test_bridge_env_int_invalid_string(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_INT", "banana")
    assert bridge._env_int("BRIDGE_TEST_INT", 7, 1, 20) == 7
