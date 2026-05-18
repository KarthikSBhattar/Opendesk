#!/usr/bin/env python3
"""Computer use via a plan-act-observe loop."""

import io
import json
import os
import re
import subprocess
import time
from typing import Callable


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


MAX_AGENT_STEPS = _env_int("OPEN_DESK_MAX_AGENT_STEPS", 12, 4, 80)
SCREENSHOT_MAX_WIDTH = _env_int("OPEN_DESK_SCREENSHOT_MAX_WIDTH", 900, 640, 1600)

ROUTER_SYSTEM = (
    "You are the request router for a desktop voice assistant. Output ONLY valid JSON.\n\n"
    "Choose the FIRST matching type:\n"
    "1. answer — no computer interaction needed (facts, questions, math).\n"
    "2. direct_action — the entire request is one single-step OS operation that needs NO visual verification. "
    "Use this for: keyboard shortcuts, opening a named app, opening a specific URL, "
    "scrolling, typing known text. Execute immediately without inspecting the screen. "
    "Do NOT use direct_action for compound requests like opening a new tab and then searching, "
    "opening an app and then doing something inside it, or any request with a follow-up action.\n"
    "3. computer_task — multi-step workflow OR anything that requires seeing the screen first "
    "(find an element, read content, navigate unknown UI).\n\n"
    "Schemas:\n"
    '{"type":"answer","text":"concise answer"}\n'
    '{"type":"direct_action","action":"key","key":"cmd+t","speak":"natural narration"}\n'
    '{"type":"direct_action","action":"open_app","app":"Application Name","speak":"natural narration"}\n'
    '{"type":"direct_action","action":"open_url","url":"https://...","speak":"natural narration"}\n'
    '{"type":"direct_action","action":"type","text":"text to type","speak":"natural narration"}\n'
    '{"type":"direct_action","action":"scroll","direction":"down","amount":3,"speak":"natural narration"}\n'
    '{"type":"computer_task","goal":"what success means","plan":["step 1","step 2"],'
    '"speak":"brief natural spoken opener, 1 sentence, first-person"}'
)

AGENT_SYSTEM = (
    "You are a careful Mac computer-use agent. A screenshot, task, and previous observations are provided.\n"
    "Output ONLY the next single step as valid JSON. Do not output markdown.\n\n"
    "Rules:\n"
    "- Before choosing an action, think silently through the remaining goal, the visible state, the focused control, "
    "the exact target, what could go wrong, and how the next observation should confirm progress. Do not include "
    "this reasoning in the JSON.\n"
    "- Do not optimize for speed over correctness. If a slower, lower-risk action is more reliable, choose it.\n"
    "- Do one small, reversible action at a time.\n"
    "- Treat the initial plan as advisory. Re-plan from the current screenshot whenever the page, app, "
    "or visible state differs from what the plan expected.\n"
    "- If you are uncertain between two actions, prefer the one most grounded in visible evidence, or ask when "
    "the missing detail is user-specific.\n"
    "- If an unexpected modal, overlay, prompt, banner, or blocking UI prevents the next planned action, "
    "deal with that visible blocker first using the least risky available action.\n"
    "- Use the previous observations. Do not repeat a failed click or key sequence unless the screen changed.\n"
    "- If a click appears to miss or the screen did not change as expected, inspect the new screenshot and "
    "re-aim at the visible target instead of giving up.\n"
    "- Never invent, assume, or fabricate user-specific form values. If a required value was not provided "
    "in the request or previous conversation, ask for it before typing or submitting.\n"
    "- For forms, ask for missing required fields one at a time. After typing a value, use the trace and "
    "current screenshot together to decide whether to continue.\n"
    "- When correcting a field or replacing existing contents, use action type with mode replace.\n"
    "- Text entry changed screen in the trace means the UI visibly changed after typing; use the current screenshot "
    "to decide the next step. Do not retype unless the field appears wrong.\n"
    "- Text entry unverified means typing produced no meaningful visual change. Refocus the intended field and retry "
    "only if the field appears empty or wrong; do not loop.\n"
    "- If the same approach fails twice, switch strategy: use search, address bars, menus, app launch, or ask for help.\n"
    "- Avoid idle loops. Do not wait twice in a row unless the screen is visibly loading.\n"
    "- Prefer deterministic OS actions like open_url and open_app when they directly satisfy the task and app focus is uncertain.\n"
    "- For scrolling, use distance small, medium, large, or page. Use large/page when searching through long content; "
    "use small only for fine positioning near a target.\n"
    "- When the task is mostly complete, finish with done instead of polishing indefinitely.\n"
    "- Use drag when the task requires click-and-hold movement, such as rearranging items, resizing windows, "
    "selecting a range, or any UI that requires holding the mouse button while moving.\n"
    "- For clicks, return a tight bounding box around the visible target. Coordinates are normalized 0-1000 "
    "as [y1, x1, y2, x2].\n"
    "- Always populate the speak field with natural first-person narration of what you are doing right now "
    "(e.g. 'Opening Safari', 'I can see the button, clicking it now', 'Typing your message'). "
    "Be brief and conversational — as if talking a person through it live.\n"
    "- If the task needs clarification, stop with action ask.\n"
    "- If the task is complete, stop with action done.\n\n"
    "Schemas:\n"
    '{"action":"click","box_2d":[y1,x1,y2,x2],"label":"visible target","speak":"natural narration"}\n'
    '{"action":"type","text":"text to enter","mode":"insert or replace","speak":"natural narration"}\n'
    '{"action":"key","key":"cmd+space","speak":"natural narration"}\n'
    '{"action":"open_url","url":"url to open","speak":"natural narration"}\n'
    '{"action":"open_app","app":"Application Name","speak":"natural narration"}\n'
    '{"action":"scroll","direction":"down","amount":3,"distance":"large","speak":"natural narration"}\n'
    '{"action":"move_mouse","box_2d":[y1,x1,y2,x2],"speak":"natural narration"}\n'
    '{"action":"drag","from_box_2d":[y1,x1,y2,x2],"to_box_2d":[y1,x1,y2,x2],"duration":0.5,"speak":"natural narration"}\n'
    '{"action":"wait","seconds":1,"speak":"natural narration"}\n'
    '{"action":"ask","question":"specific question for the user"}\n'
    '{"action":"done","result":"short natural summary of what was accomplished"}'
)


def screen_size() -> tuple[int, int]:
    try:
        import pyautogui
        s = pyautogui.size()
        return s.width, s.height
    except Exception:
        return 1920, 1080


def take_screenshot(max_width: int = SCREENSHOT_MAX_WIDTH) -> bytes:
    """Capture screen, resize to max_width if needed, return PNG bytes."""
    try:
        import pyautogui
        from PIL import Image
        img = pyautogui.screenshot()
    except Exception:
        import subprocess, tempfile
        path = tempfile.mktemp(suffix=".png")
        try:
            subprocess.run(["screencapture", "-x", path], check=True)
            from PIL import Image
            img = Image.open(path)
            img.load()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    w, h = img.size
    if w > max_width:
        from PIL import Image
        img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def parse_action(text: str) -> dict | None:
    """Extract JSON action dict from model output. Returns None on failure."""
    text = text.strip()
    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    # Find first {...} block
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def _clip_unit(value: float) -> float:
    return max(0.0, min(1000.0, value))


def _normalized_box_center(box: list | None) -> tuple[float, float] | None:
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        y1, x1, y2, x2 = [_clip_unit(float(v)) for v in box]
    except (TypeError, ValueError):
        return None
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 < x1:
        x1, x2 = x2, x1
    return (x1 + x2) / 2, (y1 + y2) / 2


def describe_action(action: dict) -> str:
    """Return a concise user-facing label for an action."""
    speak = str(action.get("speak", "")).strip()
    if speak:
        return speak

    kind = str(action.get("action", ""))
    if kind == "click":
        return f"Clicking {action.get('label', 'the target')}"
    if kind == "type":
        text = str(action.get("text", ""))
        return f"Typing {text[:48]}".rstrip()
    if kind == "key":
        return f"Pressing {action.get('key', 'a key')}"
    if kind == "open_url":
        return f"Opening {action.get('url', 'a URL')}"
    if kind == "open_app":
        return f"Opening {action.get('app', 'an app')}"
    if kind == "move_mouse":
        return f"Moving mouse to {action.get('label', 'target')}"
    if kind == "drag":
        return f"Dragging from {action.get('from_label', 'source')} to {action.get('to_label', 'target')}"
    if kind == "scroll":
        return f"Scrolling {action.get('direction', 'down')}"
    if kind == "wait":
        return "Waiting for the screen to update"
    return kind or "Working"


# pyautogui on macOS requires "command" not "cmd", "option" not "alt", etc.
_KEY_ALIASES: dict[str, str] = {
    "cmd": "command",
    "alt": "option",
    "opt": "option",
    "ctrl": "ctrl",
}


def _normalize_keys(key_str: str) -> list[str]:
    return [_KEY_ALIASES.get(p, p) for p in key_str.lower().split("+")]


def _type_text(text: str, replace: bool = False) -> tuple[bool, str]:
    """Type text into the focused control without relying on clipboard paste."""
    import pyautogui
    try:
        if replace:
            pyautogui.hotkey("command", "a")
            time.sleep(0.05)
        pyautogui.write(text, interval=0.02)
        return False, "typed with direct keyboard events; verify from the current screen"
    except Exception as exc:
        return False, f"typing failed: {exc}"


_SCROLL_DISTANCE_MULTIPLIERS: dict[str, int] = {
    "small": 5,
    "medium": 10,
    "large": 18,
    "page": 28,
}


def _scroll_clicks(action: dict) -> int:
    distance = str(action.get("distance", "large")).lower()
    try:
        amount = int(action.get("amount", 1))
    except (TypeError, ValueError):
        amount = 1
    amount = max(1, min(5, amount))
    multiplier = _SCROLL_DISTANCE_MULTIPLIERS.get(distance, _SCROLL_DISTANCE_MULTIPLIERS["large"])
    return amount * multiplier


def execute_action(action: dict, broadcast: Callable, sw: int, sh: int) -> str:
    """Execute a parsed action dict. Returns result string."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
    except ImportError:
        return "Error: pyautogui not installed"

    kind = str(action.get("action", "")).lower()
    label_text = describe_action(action)

    try:
        if kind == "click":
            center = _normalized_box_center(action.get("box_2d"))
            if center is None:
                try:
                    center = (_clip_unit(float(action.get("x", 500))), _clip_unit(float(action.get("y", 500))))
                except (TypeError, ValueError):
                    return "Invalid click target"
            nx, ny = center
            px = int(nx / 1000 * sw)
            py = int(ny / 1000 * sh)
            label = action.get("label", f"({px},{py})")
            broadcast({"type": "agent_action", "label": label_text})
            btn = action.get("button", "left")
            pyautogui.moveTo(px, py, duration=0.08)
            if btn == "double":
                pyautogui.doubleClick(px, py)
            elif btn == "right":
                pyautogui.rightClick(px, py)
            else:
                pyautogui.click(px, py)
            time.sleep(0.8)
            return f"Clicked {label} at ({px},{py})"

        if kind == "type":
            text = str(action.get("text", ""))
            mode = str(action.get("mode", "insert")).lower()
            replace = mode in {"replace", "overwrite", "select_all"} or bool(action.get("replace"))
            broadcast({"type": "agent_action", "label": label_text})
            verified, reason = _type_text(text, replace=replace)
            time.sleep(0.4)
            if verified:
                return f"Typed and verified: {reason}"
            return f"Typed but not verified: {reason}"

        if kind == "key":
            key = str(action.get("key", ""))
            parts = _normalize_keys(key)
            if not parts or not parts[0]:
                return "Invalid key"
            broadcast({"type": "agent_action", "label": label_text})
            if len(parts) > 1:
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(parts[0])
            time.sleep(0.5)
            return f"Pressed: {key}"

        if kind == "open_url":
            url = str(action.get("url", "")).strip()
            if not url:
                return "Invalid URL"
            broadcast({"type": "agent_action", "label": label_text})
            subprocess.run(["open", url], check=True)
            time.sleep(1.5)
            return f"Opened URL: {url}"

        if kind == "open_app":
            app = str(action.get("app", "")).strip()
            if not app:
                return "Invalid app"
            broadcast({"type": "agent_action", "label": label_text})
            subprocess.run(["open", "-a", app], check=True)
            time.sleep(1.5)
            return f"Opened app: {app}"

        if kind == "scroll":
            direction = str(action.get("direction", "down"))
            distance = str(action.get("distance", "large")).lower()
            scroll_units = _scroll_clicks(action)
            clicks = scroll_units if direction == "up" else -scroll_units
            broadcast({"type": "agent_action", "label": label_text})
            pyautogui.scroll(clicks)
            time.sleep(0.7)
            return f"Scrolled {direction} {distance} ({scroll_units} units)"

        if kind == "move_mouse":
            center = _normalized_box_center(action.get("box_2d"))
            if center is None:
                return "Invalid move_mouse target"
            px, py = int(center[0] / 1000 * sw), int(center[1] / 1000 * sh)
            broadcast({"type": "agent_action", "label": label_text})
            pyautogui.moveTo(px, py, duration=0.2)
            time.sleep(0.3)
            return f"Moved mouse to ({px},{py})"

        if kind == "drag":
            from_center = _normalized_box_center(action.get("from_box_2d"))
            to_center = _normalized_box_center(action.get("to_box_2d"))
            if from_center is None or to_center is None:
                return "Invalid drag: missing from_box_2d or to_box_2d"
            fx, fy = int(from_center[0] / 1000 * sw), int(from_center[1] / 1000 * sh)
            tx, ty = int(to_center[0] / 1000 * sw), int(to_center[1] / 1000 * sh)
            duration = float(action.get("duration", 0.5))
            duration = max(0.1, min(3.0, duration))
            broadcast({"type": "agent_action", "label": label_text})
            pyautogui.moveTo(fx, fy, duration=0.08)
            pyautogui.mouseDown()
            time.sleep(0.05)
            pyautogui.moveTo(tx, ty, duration=duration)
            pyautogui.mouseUp()
            time.sleep(0.5)
            return f"Dragged ({fx},{fy}) → ({tx},{ty}) over {duration:.1f}s"

        if kind == "wait":
            seconds = float(action.get("seconds", 1))
            seconds = max(0.2, min(2.0, seconds))
            broadcast({"type": "agent_action", "label": label_text})
            time.sleep(seconds)
            return f"Waited {seconds:.1f}s"
    except Exception as exc:
        return f"Action failed: {kind or 'unknown'}: {exc}"

    return f"Unknown action: {kind}"
