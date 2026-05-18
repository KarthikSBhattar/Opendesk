#!/usr/bin/env python3
"""
OpenDesk voice bridge — runs three threads:

  1. HTTP SSE server  (:38476/events)
     Broadcasts JSON events to all connected React clients.

  2. STT thread  (run_stt)
     Listens on the microphone via RealtimeSTT (Whisper). Emits realtime
     preview events while the user is speaking, then finalizes the utterance
     and hands it to call_ai().

  3. TTS worker thread  (_tts_worker)
     Dequeues text from _TTS_QUEUE and streams PCM16 audio from Deepgram
     back to the frontend via tts_audio SSE events.

call_ai() routing:
  • answer        — streams tokens word-by-word, enqueues TTS
  • direct_action — executes a single OS action (key, open, type, scroll)
  • computer_task — enters _agent_loop(): screenshot → action → observe → repeat

The Rust shell (src-tauri/src/lib.rs) spawns this script and injects all
.env variables into the subprocess environment before it starts.
"""
import json
import math
import os
import queue
import re
import signal
import sys
import threading
import time
import base64
import concurrent.futures
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import certifi
    import ssl

    _CERTIFI_CA = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _CERTIFI_CA)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CERTIFI_CA)

    def _certifi_https_context(*args, **kwargs):
        kwargs.setdefault("cafile", _CERTIFI_CA)
        return ssl.create_default_context(*args, **kwargs)

    ssl._create_default_https_context = _certifi_https_context
except Exception:
    pass

PORT = int(os.environ.get("OPEN_DESK_STT_PORT", "38476"))
CLIENTS: set[queue.Queue] = set()
CLIENTS_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
LATEST_STATUS: dict | None = None
LATEST_STATUS_LOCK = threading.Lock()
STICKY_EVENT_TYPES = {"server_ready", "loading", "listening", "error"}
STT_LOAD_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_STT_LOAD_TIMEOUT", "90"))
MIC_DEVICE_WAIT_SECS = float(
    os.environ.get("OPEN_DESK_MIC_DEVICE_WAIT", "20" if sys.platform == "darwin" else "5")
)
MIC_DEVICE_RETRY_SECS = float(os.environ.get("OPEN_DESK_MIC_DEVICE_RETRY", "0.5"))
VIRTUAL_INPUT_NAME_PARTS = tuple(
    part.strip().lower()
    for part in os.environ.get(
        "OPEN_DESK_VIRTUAL_INPUT_NAMES",
        "blackhole,soundflower,loopback,vb-cable,virtual audio,aggregate device,multi-output",
    ).split(",")
    if part.strip()
)
AI_ROUTER_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_AI_ROUTER_TIMEOUT", "25"))
AI_ACTION_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_AI_ACTION_TIMEOUT", "75"))
AI_CHECK_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_AI_CHECK_TIMEOUT", "25"))
AI_FINAL_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_AI_FINAL_TIMEOUT", "40"))
SCREENSHOT_TIMEOUT_SECS = float(os.environ.get("OPEN_DESK_SCREENSHOT_TIMEOUT", "8"))

UTTERANCE_DEBOUNCE_SECS = float(os.environ.get("OPEN_DESK_UTTERANCE_DEBOUNCE", "1.4"))

AI_ACTIVE = threading.Event()
AI_CANCEL = threading.Event()
AI_RUN_LOCK = threading.Lock()
CONVERSATION_HISTORY: list = []
HISTORY_LOCK = threading.Lock()
PENDING_AGENT_ASK: dict | None = None
PENDING_AGENT_ASK_LOCK = threading.Lock()
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("OPEN_DESK_TIMEOUT_WORKERS", "6")),
    thread_name_prefix="open-desk-timeout",
)

from computer_agent import (
    AGENT_SYSTEM,
    MAX_AGENT_STEPS,
    ROUTER_SYSTEM,
    describe_action,
    execute_action,
    parse_action,
    screen_size,
    take_screenshot,
)

# ── Screen cache (request-time prefetch) ──────────────────────────────────────
_SCREEN_CACHE_LOCK = threading.Lock()
_SCREEN_CACHE: dict = {"shot": None, "ts": 0.0}


def _refresh_screen_cache() -> None:
    """Capture a fresh screenshot and store it in the shared cache."""
    try:
        shot = _run_with_timeout(take_screenshot, SCREENSHOT_TIMEOUT_SECS, "Screenshot")
        with _SCREEN_CACHE_LOCK:
            _SCREEN_CACHE["shot"] = shot
            _SCREEN_CACHE["ts"] = time.time()
    except Exception:
        pass


def _get_screen(max_age: float = 3.0) -> bytes:
    """Return cached screenshot if fresh enough, otherwise capture a new one."""
    with _SCREEN_CACHE_LOCK:
        shot = _SCREEN_CACHE["shot"]
        age = time.time() - float(_SCREEN_CACHE["ts"])
        if shot is not None and age < max_age:
            return shot  # type: ignore[return-value]
    return _run_with_timeout(take_screenshot, SCREENSHOT_TIMEOUT_SECS, "Screenshot")


def _run_with_timeout(fn, timeout_secs: float, label: str, *args, **kwargs):
    future = _TIMEOUT_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=max(1.0, timeout_secs))
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"{label} timed out after {timeout_secs:.0f}s") from exc


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _list_input_devices() -> list[dict]:
    import pyaudio

    audio = pyaudio.PyAudio()
    try:
        devices = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if info.get("maxInputChannels", 0) > 0:
                devices.append(info)
        return devices
    finally:
        audio.terminate()


def _microphone_unavailable_message(last_error: Exception | None = None) -> str:
    details = f": {last_error}" if last_error else ""
    if sys.platform == "darwin":
        return (
            "No microphone input devices are visible to Python. "
            "Grant microphone access to OpenDesk and Python in System Settings > "
            f"Privacy & Security > Microphone, then restart OpenDesk{details}"
        )

    return f"No microphone input devices visible to Python{details}"


def _device_name(device: dict) -> str:
    return str(device.get("name", f"device {device.get('index', '?')}"))


def _is_virtual_input(device: dict) -> bool:
    name = _device_name(device).lower()
    return any(part in name for part in VIRTUAL_INPUT_NAME_PARTS)


def _format_device_list(devices: list[dict]) -> str:
    return ", ".join(
        f"{device.get('index', '?')}: {_device_name(device)}" for device in devices
    )


def _select_input_device(devices: list[dict]) -> tuple[int | None, str | None]:
    requested_index = os.environ.get("OPEN_DESK_STT_INPUT_DEVICE_INDEX")
    if requested_index:
        try:
            selected_index = int(requested_index)
        except ValueError:
            raise ValueError(
                f"OPEN_DESK_STT_INPUT_DEVICE_INDEX must be an integer: {requested_index}"
            )

        for device in devices:
            if int(device.get("index", -1)) == selected_index:
                return selected_index, _device_name(device)
        raise ValueError(
            f"OPEN_DESK_STT_INPUT_DEVICE_INDEX={selected_index} is not visible. "
            f"Visible inputs: {_format_device_list(devices)}"
        )

    name_match = os.environ.get("OPEN_DESK_STT_INPUT_DEVICE_MATCH", "").strip().lower()
    if name_match:
        for device in devices:
            if name_match in _device_name(device).lower():
                return int(device["index"]), _device_name(device)
        raise ValueError(
            f"No input device matches OPEN_DESK_STT_INPUT_DEVICE_MATCH={name_match!r}. "
            f"Visible inputs: {_format_device_list(devices)}"
        )

    physical_devices = [device for device in devices if not _is_virtual_input(device)]
    if physical_devices:
        device = physical_devices[0]
        return int(device["index"]), _device_name(device)

    return None, None


def _only_virtual_inputs_message(devices: list[dict]) -> str:
    device_list = _format_device_list(devices)
    return (
        f"Only virtual audio inputs are visible ({device_list}). "
        "Select a real microphone in macOS input settings or grant microphone "
        "access to OpenDesk/Python, then restart OpenDesk."
    )


# ── TTS ──────────────────────────────────────────────────────────────────────
_TTS_QUEUE: queue.Queue = queue.Queue()
_TTS_STOP = threading.Event()
_TTS_SAMPLE_RATE = 24000


def _deepgram_speak(text: str) -> None:
    api_key = os.environ.get("DEEPGRAM_TTS_KEY", "")
    if not api_key:
        sys.stderr.write("TTS skipped: DEEPGRAM_TTS_KEY not set\n")
        return
    try:
        import httpx

        model = os.environ.get("OPEN_DESK_TTS_MODEL", "aura-2-thalia-en")
        url = f"https://api.deepgram.com/v1/speak?model={model}&encoding=linear16&sample_rate={_TTS_SAMPLE_RATE}"
        headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
        playback_id = f"{time.time_ns()}"

        broadcast({"type": "tts_start", "id": playback_id, "sampleRate": _TTS_SAMPLE_RATE})
        with httpx.stream("POST", url, headers=headers, json={"text": text}, timeout=15) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=4096):
                if _TTS_STOP.is_set():
                    broadcast({"type": "tts_stop", "id": playback_id})
                    break
                if chunk:
                    broadcast({
                        "type": "tts_audio",
                        "id": playback_id,
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    })
            else:
                broadcast({"type": "tts_end", "id": playback_id})
    except Exception as exc:
        sys.stderr.write(f"TTS error: {exc}\n")


def _tts_worker() -> None:
    while not STOP_EVENT.is_set():
        try:
            text = _TTS_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        if not _TTS_STOP.is_set():
            _deepgram_speak(text)
        _TTS_QUEUE.task_done()


def tts_interrupt() -> None:
    _TTS_STOP.set()
    broadcast({"type": "tts_stop"})
    while True:
        try:
            _TTS_QUEUE.get_nowait()
            _TTS_QUEUE.task_done()
        except queue.Empty:
            break


def tts_resume() -> None:
    _TTS_STOP.clear()


def tts_enqueue(text: str) -> None:
    text = text.strip()
    if text:
        _TTS_QUEUE.put(text)


def _patch_torch_hub_noninteractive() -> None:
    """Prevent torch.hub from prompting when this bridge runs with closed stdin."""
    try:
        import torch.hub
    except Exception:
        return

    original_load = torch.hub.load
    if getattr(original_load, "_open_desk_noninteractive", False):
        return

    def load_without_prompt(*args, **kwargs):
        kwargs.setdefault("trust_repo", True)
        return original_load(*args, **kwargs)

    load_without_prompt._open_desk_noninteractive = True  # type: ignore[attr-defined]
    torch.hub.load = load_without_prompt


def broadcast(payload: dict) -> None:
    payload.setdefault("ts", time.time())
    if payload.get("type") in STICKY_EVENT_TYPES:
        with LATEST_STATUS_LOCK:
            global LATEST_STATUS
            LATEST_STATUS = dict(payload)

    with CLIENTS_LOCK:
        clients = list(CLIENTS)

    for client in clients:
        try:
            client.put_nowait(payload)
        except queue.Full:
            pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class EventsHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/events":
            self.send_response(404)
            self.end_headers()
            return

        messages: queue.Queue = queue.Queue(maxsize=256)
        with CLIENTS_LOCK:
            CLIENTS.add(messages)

        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            send({"type": "bridge_connected"})
            with LATEST_STATUS_LOCK:
                latest_status = dict(LATEST_STATUS) if LATEST_STATUS else None
            if latest_status:
                send(latest_status)
            while not STOP_EVENT.is_set():
                try:
                    send(messages.get(timeout=10))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with CLIENTS_LOCK:
                CLIENTS.discard(messages)


def run_http_server() -> None:
    try:
        server = ReusableThreadingHTTPServer(("127.0.0.1", PORT), EventsHandler)
    except OSError as exc:
        sys.stderr.write(f"Failed to bind event server on port {PORT}: {exc}\n")
        STOP_EVENT.set()
        return

    server.timeout = 0.5
    broadcast({"type": "server_ready", "port": PORT})

    while not STOP_EVENT.is_set():
        server.handle_request()

    server.server_close()


def _vertex_api_key() -> str | None:
    return os.environ.get("GOOGLE_CLOUD_API_KEY")


def _requires_global_vertex_endpoint(*models: str) -> bool:
    for model in models:
        normalized = model.strip().lower()
        if not normalized:
            continue
        if "maas" in normalized or normalized.startswith("gemma-"):
            return True
    return False


def _vertex_ai_location(force_global: bool = False) -> str:
    configured_location = (
        os.environ.get("GOOGLE_CLOUD_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_REGION")
    )
    return "global" if force_global else configured_location or "global"


def _set_vertex_location_env(location: str) -> None:
    os.environ["GOOGLE_CLOUD_LOCATION"] = location


def _vertex_ai_config(force_global: bool = False) -> tuple[str, str]:
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
    )
    location = _vertex_ai_location(force_global=force_global)

    if not project:
        if force_global:
            raise RuntimeError(
                "This model requires the Vertex AI global endpoint, which is not available "
                "through Vertex AI Express/API-key mode. Set GOOGLE_CLOUD_PROJECT and "
                "authenticate with Application Default Credentials, or choose an Express-compatible model."
            )
        raise RuntimeError(
            "GOOGLE_CLOUD_API_KEY is not set for Vertex AI Express mode. "
            "Set it, or set GOOGLE_CLOUD_PROJECT and authenticate with Application Default Credentials."
        )
    return project, location


def _create_vertex_genai_client(genai, types, *models: str):
    force_global = _requires_global_vertex_endpoint(*models)
    api_key = _vertex_api_key()
    if force_global:
        project, location = _vertex_ai_config(force_global=True)
        _set_vertex_location_env(location)
        return genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(api_version="v1"),
        )

    if api_key:
        return genai.Client(
            vertexai=True,
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1"),
        )

    project, location = _vertex_ai_config(force_global=force_global)
    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(api_version="v1"),
    )


def call_ai(text: str) -> None:
    AI_RUN_LOCK.acquire()
    final_text: list[str] = []
    types = None
    AI_CANCEL.clear()
    AI_ACTIVE.set()

    try:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:
            broadcast({"type": "error", "message": f"google-genai not installed: {exc}"})
            return

        types = genai_types
        broadcast({"type": "ai_start"})

        with HISTORY_LOCK:
            user_content = types.Content(role="user", parts=[types.Part.from_text(text=text)])
            CONVERSATION_HISTORY.append(user_content)
            contents = list(CONVERSATION_HISTORY)
            current_contents = [user_content]

        model = os.environ.get("OPEN_DESK_AI_MODEL", "google/gemma-4-26b-a4b-it-maas")
        router_model = os.environ.get("OPEN_DESK_ROUTER_MODEL", model)
        client = _create_vertex_genai_client(genai, types, model, router_model)

        pending_ask = _pop_pending_agent_ask()
        if pending_ask:
            question = str(pending_ask.get("question") or "the previous question")
            original_task = str(pending_ask.get("task") or "")
            goal = str(pending_ask.get("goal") or original_task or text)
            plan = pending_ask.get("plan") if isinstance(pending_ask.get("plan"), list) else []
            trace = pending_ask.get("trace") if isinstance(pending_ask.get("trace"), list) else []
            if _looks_like_partial_answer(text):
                repeat_question = f"I only heard {text!r}. Can you say that again?"
                return _agent_ask(repeat_question, original_task, goal, plan, trace)
            resumed_task = (
                f"{original_task}\n\n"
                f"The agent paused to ask: {question}\n"
                f"The user answered: {text}\n"
                "Continue the original computer task using this answer."
            )
            route = {
                "type": "computer_task",
                "goal": goal,
                "plan": plan,
                "trace": trace,
            }
            broadcast({"type": "agent_start"})
            broadcast({"type": "agent_thought", "text": "Got it, continuing. "})
            tts_enqueue("Got it, continuing.")
            result = _agent_loop(resumed_task, route, client, model, types)
            if result:
                final_text.append(result)
            return

        router_resp = _generate_json(
            client,
            router_model,
            contents,
            types,
            ROUTER_SYSTEM,
            AI_ROUTER_TIMEOUT_SECS,
        )
        raw = router_resp.text or ""
        route = parse_action(raw) or {}
        route = _promote_compound_direct_action(text, route)

        if route.get("type") == "direct_action":
            # Single-step action — skip agent loop and screenshot entirely
            speak_text = str(route.get("speak", "")).strip()
            if speak_text:
                tts_enqueue(speak_text)
            broadcast({"type": "agent_start"})
            sw, sh = screen_size()
            observation = execute_action(route, broadcast, sw, sh)
            final_text.append(observation)
            broadcast({"type": "agent_done", "result": observation})

        elif route.get("type") == "computer_task":
            # Multi-step workflow — prime screenshot cache now that we know we need it
            threading.Thread(target=_refresh_screen_cache, daemon=True).start()
            opener = str(route.get("speak", "")).strip()
            if opener:
                tts_enqueue(opener)
            broadcast({"type": "agent_start"})
            result = _agent_loop(text, route, client, model, types)
            if result:
                final_text.append(result)
        else:
            # Plain answer from router response
            answer = route.get("text") or raw
            final_text.append(answer)
            _broadcast_text(answer)
            tts_enqueue(answer)

    except Exception as exc:
        broadcast({"type": "error", "message": f"AI error: {exc}"})
    finally:
        cancelled = AI_CANCEL.is_set()
        AI_ACTIVE.clear()
        AI_CANCEL.clear()
        if types is not None:
            with HISTORY_LOCK:
                if cancelled:
                    if CONVERSATION_HISTORY and CONVERSATION_HISTORY[-1].role == "user":
                        CONVERSATION_HISTORY.pop()
                elif not final_text:
                    if CONVERSATION_HISTORY and CONVERSATION_HISTORY[-1].role == "user":
                        CONVERSATION_HISTORY.pop()
                elif final_text:
                    CONVERSATION_HISTORY.append(
                        types.Content(
                            role="model",
                            parts=[types.Part.from_text(text="".join(final_text))],
                        )
                    )
        broadcast({"type": "ai_done"})
        AI_RUN_LOCK.release()


def _broadcast_text(text: str) -> None:
    words = text.split()
    for i, word in enumerate(words):
        broadcast({"type": "ai_token", "text": word + (" " if i < len(words) - 1 else "")})


def _looks_like_partial_answer(text: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", text or "")
    return len(cleaned) < 2


def _promote_compound_direct_action(text: str, route: dict) -> dict:
    if route.get("type") != "direct_action":
        return route

    action = str(route.get("action", "")).lower()
    lowered = f" {text.lower()} "
    asks_for_new_tab = bool(re.search(r"\b(new|open)\s+(a\s+)?(new\s+)?tab\b", lowered))
    asks_for_followup = bool(
        re.search(
            r"\b(search|google|look\s+up|find|type|enter|write|go\s+to|navigate|do)\b",
            lowered,
        )
    )
    has_compound_joiner = bool(re.search(r"\b(and|then|after\s+that|afterwards)\b", lowered))

    if action in {"key", "open_app"} and has_compound_joiner and (asks_for_new_tab or asks_for_followup):
        return {
            "type": "computer_task",
            "goal": text,
            "plan": [
                "Prepare the requested app or tab",
                "Complete the requested follow-up action",
            ],
            "speak": str(route.get("speak") or "I will handle the full request."),
        }

    return route


def _json_config(
    types,
    model: str,
    system_instruction: str,
    allow_thinking_config: bool = True,
    response_mime_type: str | None = "application/json",
):
    kwargs = {
        "system_instruction": system_instruction,
        "temperature": 0,
    }
    if response_mime_type:
        kwargs["response_mime_type"] = response_mime_type
    if allow_thinking_config:
        normalized_model = model.strip().lower().split("/")[-1]
        thinking_level = os.environ.get("OPEN_DESK_THINKING_LEVEL")
        thinking_budget = _env_int("OPEN_DESK_THINKING_BUDGET", 1024, 0, 32768)
        if thinking_level or normalized_model.startswith("gemini-3"):
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=thinking_level or "HIGH"
            )
        else:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    return types.GenerateContentConfig(**kwargs)


def _generate_json(
    client,
    model: str,
    contents,
    types,
    system_instruction: str,
    timeout_secs: float,
):
    def is_internal_error(exc: Exception) -> bool:
        message = str(exc)
        return "500 INTERNAL" in message or "Internal error encountered" in message

    def is_thinking_config_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "thinking" in message and "not supported" in message

    try:
        return _run_with_timeout(
            client.models.generate_content,
            timeout_secs,
            f"AI request to {model}",
            model=model,
            contents=contents,
            config=_json_config(types, model, system_instruction, allow_thinking_config=True),
        )
    except Exception as exc:
        if not is_thinking_config_error(exc) and not is_internal_error(exc):
            raise

    # Some Gemma backend paths intermittently 500 when JSON mode is paired
    # with explicit thinking controls. Keep the same model and request, but relax
    # the optional config before surfacing the failure to the UI.
    try:
        return _run_with_timeout(
            client.models.generate_content,
            timeout_secs,
            f"AI request to {model}",
            model=model,
            contents=contents,
            config=_json_config(types, model, system_instruction, allow_thinking_config=False),
        )
    except Exception as exc:
        if not is_internal_error(exc):
            raise

    return _run_with_timeout(
        client.models.generate_content,
        timeout_secs,
        f"AI request to {model}",
        model=model,
        contents=contents,
        config=_json_config(
            types,
            model,
            system_instruction,
            allow_thinking_config=False,
            response_mime_type=None,
        ),
    )


def _format_agent_trace(trace: list[dict]) -> str:
    if not trace:
        return "No previous steps."
    lines = []
    for item in trace[-12:]:
        step = item.get("step", "?")
        action = item.get("action", "unknown")
        label = item.get("label", "")
        observation = item.get("observation", "")
        lines.append(f"{step}. {action}: {label} -> {observation}")
    return "\n".join(lines)


def _action_signature(action: dict) -> str:
    """Return a compact signature for loop/stall detection."""
    kind = str(action.get("action", "")).lower()
    if kind == "click":
        box = action.get("box_2d")
        if isinstance(box, list) and len(box) == 4:
            try:
                y1, x1, y2, x2 = [round(float(v) / 25) * 25 for v in box]
                return f"click:{y1}:{x1}:{y2}:{x2}:{str(action.get('label', '')).lower()[:32]}"
            except (TypeError, ValueError):
                pass
        return f"click:{action.get('x')}:{action.get('y')}:{str(action.get('label', '')).lower()[:32]}"
    if kind == "key":
        return f"key:{str(action.get('key', '')).lower()}"
    if kind in {"open_app", "open_url"}:
        return f"{kind}:{str(action.get('app') or action.get('url') or '').lower()}"
    if kind == "wait":
        return "wait"
    if kind == "scroll":
        return f"scroll:{str(action.get('direction', 'down')).lower()}"
    if kind == "type":
        return f"type:{str(action.get('text', ''))[:80]}:{str(action.get('label', '')).lower()[:32]}"
    return kind


def _is_repeated_stall(kind: str, signature: str, recent_signatures: list[str]) -> bool:
    if not signature or not recent_signatures:
        return False
    if kind == "wait":
        return recent_signatures[-1] == signature
    if kind == "click":
        return recent_signatures.count(signature) >= 2
    if kind in {"key", "open_app", "open_url"}:
        return signature in recent_signatures[-2:]
    if kind == "scroll":
        return len(recent_signatures) >= 3 and all(sig == signature for sig in recent_signatures[-3:])
    return False


def _screen_changed(before: bytes, after: bytes) -> tuple[bool, str]:
    if not before or not after:
        return False, "missing screenshot for visual comparison"
    try:
        from PIL import Image, ImageChops

        before_img = Image.open(BytesIO(before)).convert("RGB")
        after_img = Image.open(BytesIO(after)).convert("RGB")
        if before_img.size != after_img.size:
            after_img = after_img.resize(before_img.size)

        diff = ImageChops.difference(before_img, after_img).convert("L")
        histogram = diff.histogram()
        changed_pixels = sum(count for value, count in enumerate(histogram) if value >= 12)
        total_pixels = max(1, before_img.size[0] * before_img.size[1])
        changed_ratio = changed_pixels / total_pixels

        if changed_pixels >= 80 or changed_ratio >= 0.00015:
            return True, f"visual change detected after typing ({changed_pixels} changed pixels)"
        return False, f"no meaningful visual change detected after typing ({changed_pixels} changed pixels)"
    except Exception as exc:
        return False, f"visual comparison failed: {exc}"


def _agent_done(result: str) -> str:
    broadcast({"type": "agent_done", "result": result})
    _broadcast_text(result)
    tts_enqueue(result)
    return result


def _set_pending_agent_ask(
    question: str,
    task: str,
    goal: str,
    plan: list,
    trace: list[dict],
) -> None:
    global PENDING_AGENT_ASK
    with PENDING_AGENT_ASK_LOCK:
        PENDING_AGENT_ASK = {
            "question": question,
            "task": task,
            "goal": goal,
            "plan": list(plan),
            "trace": list(trace),
            "ts": time.time(),
        }


def _pop_pending_agent_ask() -> dict | None:
    global PENDING_AGENT_ASK
    with PENDING_AGENT_ASK_LOCK:
        pending = PENDING_AGENT_ASK
        PENDING_AGENT_ASK = None
    return pending


def _agent_ask(question: str, task: str, goal: str, plan: list, trace: list[dict]) -> str:
    _set_pending_agent_ask(question, task, goal, plan, trace)
    broadcast({"type": "agent_ask", "question": question, "result": question})
    _broadcast_text(question)
    tts_enqueue(question)
    return question


def _final_agent_assessment(
    task: str,
    goal: str,
    trace: list[dict],
    client,
    model: str,
    types,
) -> str:
    """Ask the vision model for a useful terminal status instead of exposing the step cap."""
    from google.genai import types as _types

    try:
        shot = _get_screen(max_age=1.0)
    except Exception:
        shot = b""

    prompt = (
        f"User request: {task}\n"
        f"Success goal: {goal}\n"
        f"Recent observations:\n{_format_agent_trace(trace)}\n\n"
        "The action budget is exhausted. Inspect the current screen and output exactly one JSON object.\n"
        "If the task appears complete, output done with a concise result. "
        "If it is not complete, output ask with the single most useful clarification or manual help request. "
        "Do not output another action."
    )
    parts = []
    if shot:
        parts.append(_types.Part.from_bytes(data=shot, mime_type="image/png"))
    parts.append(_types.Part.from_text(text=prompt))

    try:
        resp = _generate_json(
            client,
            model,
            [_types.Content(role="user", parts=parts)],
            _types,
            AGENT_SYSTEM,
            AI_FINAL_TIMEOUT_SECS,
        )
        action = parse_action(resp.text or "") or {}
    except Exception:
        action = {}

    kind = str(action.get("action", "")).lower()
    if kind == "done":
        return _agent_done(str(action.get("result", "Done")))
    if kind == "ask":
        question = str(action.get("question", "I got stuck and need your help to continue."))
        return _agent_ask(question, task, goal, [], trace)
    question = "I got stuck and need your help to continue."
    return _agent_ask(question, task, goal, [], trace)


COMPLETION_CHECK_SYSTEM = (
    "You judge whether a desktop computer-use task should stop after the last action. "
    "Output ONLY valid JSON. Do not output markdown.\n\n"
    "Rules:\n"
    "- Judge the original user request literally. If the user asked to perform one action "
    "(click, type, open, press, scroll), and the trace shows that action happened, mark done.\n"
    "- Do not invent follow-up goals from the resulting page, app, or similar visible targets.\n"
    "- If the screen shows that a prior action missed, did not take effect, or a visible blocker is still "
    "preventing the requested outcome, continue so the agent can adjust from the current screen.\n"
    "- Continue only when the original requested outcome is clearly not achieved yet.\n"
    "- Ask only when user input is required to determine whether to continue.\n\n"
    "Schemas:\n"
    '{"action":"done","result":"short natural summary"}\n'
    '{"action":"continue","reason":"why more action is still required"}\n'
    '{"action":"ask","question":"specific question for the user"}'
)


def _post_action_completion_check(
    task: str,
    goal: str,
    plan: list,
    trace: list[dict],
    client,
    model: str,
    types,
) -> dict:
    """Return done/ask/continue after an executed action, without proposing another action."""
    from google.genai import types as _types

    try:
        shot = _get_screen(max_age=1.5)
    except Exception:
        shot = b""

    prompt = (
        f"User request: {task}\n"
        f"Success goal: {goal}\n"
        f"Initial plan: {json.dumps(plan[:6], ensure_ascii=False)}\n"
        f"Recent observations:\n{_format_agent_trace(trace)}\n\n"
        "Decide whether the original user request is now satisfied after the last action. "
        "Do not propose or perform another computer action."
    )
    parts = []
    if shot:
        parts.append(_types.Part.from_bytes(data=shot, mime_type="image/png"))
    parts.append(_types.Part.from_text(text=prompt))

    try:
        resp = _generate_json(
            client,
            model,
            [_types.Content(role="user", parts=parts)],
            _types,
            COMPLETION_CHECK_SYSTEM,
            AI_CHECK_TIMEOUT_SECS,
        )
        action = parse_action(resp.text or "") or {}
    except Exception as exc:
        return {"action": "continue", "reason": f"completion check failed: {exc}"}

    kind = str(action.get("action", "")).lower()
    if kind in {"done", "ask", "continue"}:
        return action
    return {"action": "continue", "reason": "completion check did not return a terminal decision"}


def _agent_loop(task: str, route: dict, client, model: str, types) -> str | None:
    """Vision agent: plan → screenshot → action → observe → repeat."""
    from google.genai import types as _types
    sw, sh = screen_size()
    goal = str(route.get("goal") or task)
    plan = route.get("plan") if isinstance(route.get("plan"), list) else []
    initial_trace = route.get("trace") if isinstance(route.get("trace"), list) else []
    trace: list[dict] = list(initial_trace)
    consecutive_parse_errors = 0
    consecutive_stalls = 0
    consecutive_text_entry_failures = 0
    unverified_type_attempts: dict[str, int] = {}
    recent_signatures: list[str] = []

    for step in range(MAX_AGENT_STEPS):
        if AI_CANCEL.is_set() or STOP_EVENT.is_set():
            broadcast({"type": "agent_cancelled"})
            return None

        plan_label = (
            str(plan[min(step, len(plan) - 1)])
            if plan
            else f"Inspecting screen for step {step + 1}"
        )
        broadcast({
            "type": "agent_step",
            "step": step + 1,
            "label": plan_label,
        })

        # Use cached screenshot — background watcher or post-action pipeline refresh
        try:
            screen_started = time.monotonic()
            shot = _get_screen(max_age=3.0)
            screen_elapsed = time.monotonic() - screen_started
            if screen_elapsed > 2.0:
                broadcast({
                    "type": "agent_thought",
                    "text": f"Screen capture took {screen_elapsed:.1f}s. ",
                })
        except Exception as exc:
            broadcast({"type": "error", "message": f"Screenshot failed: {exc}"})
            return None

        if AI_CANCEL.is_set():
            broadcast({"type": "agent_cancelled"})
            return None

        trace_text = _format_agent_trace(trace)
        strict_reminder = (
            "\nCRITICAL: Your last response was not valid JSON. Output ONLY a single JSON object, no markdown, no explanation.\n"
            if consecutive_parse_errors > 0
            else ""
        )
        recovery_reminder = ""
        if consecutive_stalls:
            recovery_reminder = (
                "\nRECOVERY MODE: The last proposed action looked like a loop or produced no progress. "
                "Choose a different strategy now. Prefer app search, address bars, menus, keyboard navigation, "
                "or ask for the missing detail. Do not repeat the rejected action.\n"
            )
        if MAX_AGENT_STEPS - step <= 4:
            recovery_reminder += (
                "\nFINAL STEPS: Finish if the goal is achieved. If not, take only high-confidence actions "
                "that directly complete the goal, or ask for help.\n"
            )
        prompt = (
            f"User request: {task}\n"
            f"Success goal: {goal}\n"
            f"Initial plan: {json.dumps(plan[:6], ensure_ascii=False)}\n"
            f"Screen size: {sw}x{sh}\n"
            f"Current step: {step + 1} of {MAX_AGENT_STEPS}. Steps remaining after this: {MAX_AGENT_STEPS - step - 1}\n"
            f"All observations so far:\n{trace_text}\n"
            f"{strict_reminder}{recovery_reminder}\n"
            "The current screenshot is authoritative. If the initial plan is no longer right, revise it. "
            "If the previous action did not work, use the visible result to choose a corrected next action. "
            "Choose the next single action now."
        )
        try:
            broadcast({
                "type": "agent_step",
                "step": step + 1,
                "label": plan_label,
            })
            model_started = time.monotonic()
            resp = _generate_json(
                client,
                model,
                [_types.Content(role="user", parts=[
                    _types.Part.from_bytes(data=shot, mime_type="image/png"),
                    _types.Part.from_text(text=prompt),
                ])],
                _types,
                AGENT_SYSTEM,
                AI_ACTION_TIMEOUT_SECS,
            )
            model_elapsed = time.monotonic() - model_started
            if model_elapsed > 5.0:
                broadcast({
                    "type": "agent_thought",
                    "text": f"Model step took {model_elapsed:.1f}s. ",
                })
        except TimeoutError as exc:
            question = f"The model is taking too long on this step ({exc}). Should I retry or change strategy?"
            return _agent_ask(question, task, goal, plan, trace)
        except Exception as exc:
            broadcast({"type": "error", "message": f"Agent error: {exc}"})
            return None

        raw = resp.text or ""
        action = parse_action(raw)

        if action is None:
            consecutive_parse_errors += 1
            if consecutive_parse_errors >= 3:
                broadcast({"type": "error", "message": "Agent failed: 3 consecutive invalid JSON responses"})
                return None
            observation = f"Invalid JSON response (attempt {consecutive_parse_errors}/3). Retrying."
            broadcast({"type": "agent_thought", "text": "Retrying… "})
            trace.append({
                "step": step + 1,
                "action": "parse_error",
                "label": raw[:120],
                "observation": observation,
            })
            continue

        consecutive_parse_errors = 0
        kind = str(action.get("action", "")).lower()
        if kind == "done":
            result = str(action.get("result", "Done"))
            return _agent_done(result)

        if kind == "ask":
            question = str(action.get("question", "I need a little more detail."))
            return _agent_ask(question, task, goal, plan, trace)

        signature = _action_signature(action)
        if kind == "type" and unverified_type_attempts.get(signature, 0) >= 2:
            return _agent_ask(
                "I tried typing that value twice, but I still cannot verify that it landed. Can you confirm whether it is filled, or click the field for me?",
                task,
                goal,
                plan,
                trace,
            )
        if _is_repeated_stall(kind, signature, recent_signatures):
            consecutive_stalls += 1
            observation = "Rejected repeated action before execution. Pick a different strategy."
            broadcast({"type": "agent_thought", "text": observation + " "})
            trace.append({
                "step": step + 1,
                "action": f"rejected_{kind or 'unknown'}",
                "label": describe_action(action),
                "observation": observation,
            })
            continue

        step_label = describe_action(action)
        speak_text = str(action.get("speak", "")).strip()
        broadcast({"type": "agent_thought", "text": step_label + " "})
        # Speak what the agent is doing — plays while the action executes
        if speak_text:
            tts_enqueue(speak_text)
        observation = execute_action(action, broadcast, sw, sh)
        # Pipeline: refresh screenshot in background while model processes observation
        threading.Thread(target=_refresh_screen_cache, daemon=True).start()
        trace.append({
            "step": step + 1,
            "action": kind or "unknown",
            "label": step_label,
            "observation": observation,
        })
        recent_signatures.append(signature)
        recent_signatures = recent_signatures[-8:]

        if observation.startswith("Unknown action") or observation.startswith("Invalid"):
            consecutive_stalls += 1
            broadcast({"type": "agent_thought", "text": observation + ". Trying a safer next step. "})
        elif observation.startswith("Action failed"):
            consecutive_stalls += 1
            broadcast({"type": "agent_thought", "text": observation + ". Switching strategy. "})
        elif kind == "type":
            after_type_shot = b""
            visual_changed = False
            visual_reason = "visual comparison did not run"
            try:
                after_type_shot = _get_screen(max_age=0.0)
                visual_changed, visual_reason = _screen_changed(shot, after_type_shot)
            except Exception as exc:
                visual_reason = f"could not compare screenshots after typing: {exc}"

            if observation.startswith("Typed and verified") or visual_changed:
                unverified_type_attempts.pop(signature, None)
                consecutive_text_entry_failures = 0
                consecutive_stalls = 0
                trace.append({
                    "step": step + 1,
                    "action": "assessment",
                    "label": "Text entry changed screen",
                    "observation": (
                        "Text entry changed the visible screen. "
                        "Use the current screenshot to decide the next step; do not retype unless the field appears wrong. "
                        f"{visual_reason}."
                    ),
                })
                completion = _post_action_completion_check(task, goal, plan, trace, client, model, types)
                completion_kind = str(completion.get("action", "")).lower()
                if completion_kind == "done":
                    return _agent_done(str(completion.get("result", "Done")))
                if completion_kind == "ask":
                    question = str(completion.get("question", "Should I keep going?"))
                    return _agent_ask(question, task, goal, plan, trace)
                if completion_kind == "continue":
                    trace.append({
                        "step": step + 1,
                        "action": "assessment",
                        "label": "Need another step",
                        "observation": str(completion.get("reason", "The requested outcome is not complete yet.")),
                    })
            else:
                consecutive_text_entry_failures += 1
                unverified_type_attempts[signature] = unverified_type_attempts.get(signature, 0) + 1
                reason = observation.removeprefix("Typed but not verified: ").strip() or "The typed value could not be mechanically verified."
                trace.append({
                    "step": step + 1,
                    "action": "assessment",
                    "label": "Text entry unverified",
                    "observation": (
                        f"{reason}. {visual_reason}. Inspect the current screenshot, refocus the intended field if needed, "
                        "and retry once only if the field appears empty or wrong."
                    ),
                })
                if unverified_type_attempts[signature] >= 3:
                    return _agent_ask(
                        "I tried typing, but the screen is not changing. Can you click the target field for me or tell me what is wrong?",
                        task,
                        goal,
                        plan,
                        trace,
                    )
                broadcast({"type": "agent_thought", "text": reason + ". I will check the screen before deciding what to do next. "})
        else:
            consecutive_text_entry_failures = 0
            consecutive_stalls = 0
            completion = _post_action_completion_check(task, goal, plan, trace, client, model, types)
            completion_kind = str(completion.get("action", "")).lower()
            if completion_kind == "done":
                return _agent_done(str(completion.get("result", "Done")))
            if completion_kind == "ask":
                question = str(completion.get("question", "Should I keep going?"))
                return _agent_ask(question, task, goal, plan, trace)
            if completion_kind == "continue":
                trace.append({
                    "step": step + 1,
                    "action": "assessment",
                    "label": "Need another step",
                    "observation": str(completion.get("reason", "The requested outcome is not complete yet.")),
                })

    return _final_agent_assessment(task, goal, trace, client, model, types)


def run_stt() -> None:
    try:
        from RealtimeSTT import AudioToTextRecorder
    except Exception as exc:
        broadcast({
            "type": "error",
            "message": f"RealtimeSTT is not available: {exc}",
        })
        return

    _patch_torch_hub_noninteractive()

    last_device_error: Exception | None = None
    device_deadline = time.monotonic() + max(0.0, MIC_DEVICE_WAIT_SECS)
    announced_device_wait = False
    selected_input_device_index: int | None = None
    selected_input_device_name: str | None = None

    while not STOP_EVENT.is_set():
        try:
            input_devices = _list_input_devices()
            last_device_error = None
        except Exception as exc:
            input_devices = []
            last_device_error = exc

        if input_devices:
            sys.stderr.write(f"Microphone input devices visible: {_format_device_list(input_devices)}\n")
            try:
                selected_input_device_index, selected_input_device_name = _select_input_device(input_devices)
            except ValueError as exc:
                broadcast({"type": "error", "message": str(exc)})
                sys.stderr.write(f"{exc}\n")
                return

            if selected_input_device_index is not None:
                sys.stderr.write(
                    f"Selected microphone input: {selected_input_device_index}: "
                    f"{selected_input_device_name}\n"
                )
                break

            if time.monotonic() >= device_deadline:
                message = _only_virtual_inputs_message(input_devices)
                broadcast({"type": "error", "message": message})
                sys.stderr.write(f"{message}\n")
                return

        if time.monotonic() >= device_deadline:
            message = _microphone_unavailable_message(last_device_error)
            broadcast({"type": "error", "message": message})
            sys.stderr.write(f"{message}\n")
            return

        if not announced_device_wait:
            broadcast({
                "type": "loading",
                "message": "Waiting for a real microphone",
            })
            sys.stderr.write("Waiting for microphone input devices to become visible\n")
            announced_device_wait = True
        time.sleep(max(0.1, MIC_DEVICE_RETRY_SECS))

    if STOP_EVENT.is_set():
        return

    model = os.environ.get("OPEN_DESK_STT_MODEL", "tiny.en")
    realtime_model = os.environ.get("OPEN_DESK_STT_REALTIME_MODEL", "tiny.en")
    device = os.environ.get("OPEN_DESK_STT_DEVICE", "cpu")
    language = os.environ.get("OPEN_DESK_STT_LANGUAGE", "en")

    broadcast({
        "type": "loading",
        "model": model,
        "realtimeModel": realtime_model,
        "device": device,
        "message": (
            f"Using microphone: {selected_input_device_name}"
            if selected_input_device_name else "Gemma is loading"
        ),
    })

    recorder_ready = threading.Event()

    def watch_recorder_load() -> None:
        if recorder_ready.wait(STT_LOAD_TIMEOUT_SECS):
            return
        message = (
            "Speech model failed to finish loading. "
            "Restart OpenDesk; if this repeats, check the STT log."
        )
        broadcast({"type": "error", "message": message})
        sys.stderr.write(f"{message}\n")
        STOP_EVENT.set()

    threading.Thread(target=watch_recorder_load, daemon=True).start()

    accumulated_parts: list[str] = []
    utterance_timer: threading.Timer | None = None
    utterance_lock = threading.Lock()
    is_speaking = False
    barge_in_active = False

    def _start_timer() -> None:
        """Must be called with utterance_lock held."""
        nonlocal utterance_timer
        if utterance_timer is not None:
            utterance_timer.cancel()
        t = threading.Timer(UTTERANCE_DEBOUNCE_SECS, finalize_utterance)
        t.daemon = True
        t.start()
        utterance_timer = t

    def _cancel_timer() -> None:
        """Must be called with utterance_lock held."""
        nonlocal utterance_timer
        if utterance_timer is not None:
            utterance_timer.cancel()
            utterance_timer = None

    def finalize_utterance() -> None:
        nonlocal accumulated_parts, barge_in_active
        with utterance_lock:
            text = " ".join(accumulated_parts).strip()
            accumulated_parts = []
            barge_in_active = False
        if not text:
            return
        tts_resume()
        broadcast({"type": "utterance_finalized", "text": text})
        call_ai(text)

    def emit_text(kind: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            broadcast({"type": kind, "text": text})

    def on_recording_start() -> None:
        nonlocal is_speaking, barge_in_active
        is_speaking = True
        if AI_ACTIVE.is_set():
            barge_in_active = True
            AI_CANCEL.set()
        tts_interrupt()
        broadcast({"type": "recording_start"})
        with utterance_lock:
            _cancel_timer()

    def on_recording_stop() -> None:
        nonlocal is_speaking
        is_speaking = False
        broadcast({"type": "recording_stop"})
        if AI_ACTIVE.is_set() and not barge_in_active:
            return
        # Start timer now; on_final will restart it if/when transcription arrives,
        # giving a full debounce window from the last spoken word.
        with utterance_lock:
            if accumulated_parts:
                _start_timer()

    last_level_emit = 0.0
    last_audio_status_emit = 0.0
    raw_audio_seen = False

    def on_recorded_chunk(chunk) -> None:
        nonlocal last_audio_status_emit, last_level_emit, raw_audio_seen

        now = time.time()

        try:
            import numpy as np

            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            if samples.size == 0:
                return

            raw_audio_seen = True

            if now - last_level_emit < 0.035:
                return

            bucket_count = 16
            buckets = np.array_split(samples, bucket_count)
            levels = []
            for bucket in buckets:
                if bucket.size == 0:
                    levels.append(0.0)
                    continue

                rms = math.sqrt(float(np.mean(np.square(bucket)))) / 32768.0
                # Speech RMS is usually small; compress into a useful visual range.
                levels.append(max(0.0, min(1.0, rms * 14.0)))

            last_level_emit = now
            broadcast({"type": "audio_levels", "levels": levels})
        except Exception as exc:
            if now - last_audio_status_emit > 2.0:
                last_audio_status_emit = now
                sys.stderr.write(f"Unable to inspect microphone audio chunk: {exc}\n")

    def on_final(text: str) -> None:
        text = (text or "").strip()
        if AI_ACTIVE.is_set() and not barge_in_active:
            return
        if not text:
            return
        emit_text("final", text)
        with utterance_lock:
            accumulated_parts.append(text)
            if not is_speaking:
                _start_timer()

    def watch_raw_audio() -> None:
        if not recorder_ready.wait(STT_LOAD_TIMEOUT_SECS):
            return
        time.sleep(5)
        if not raw_audio_seen and not STOP_EVENT.is_set():
            message = "No microphone audio is arriving"
            broadcast({"type": "audio_status", "message": message})
            sys.stderr.write(f"{message}\n")

    try:
        recorder = AudioToTextRecorder(
            model=model,
            realtime_model_type=realtime_model,
            language=language,
            device=device,
            compute_type=os.environ.get("OPEN_DESK_STT_COMPUTE_TYPE", "int8"),
            input_device_index=selected_input_device_index,
            enable_realtime_transcription=True,
            realtime_processing_pause=float(
                os.environ.get("OPEN_DESK_STT_REALTIME_PAUSE", "0.08")
            ),
            post_speech_silence_duration=float(
                os.environ.get("OPEN_DESK_STT_SILENCE_DURATION", "0.9")
            ),
            on_recording_start=on_recording_start,
            on_recording_stop=on_recording_stop,
            on_realtime_transcription_update=lambda text: (
                None if AI_ACTIVE.is_set() and not barge_in_active else emit_text("realtime_update", text)
            ),
            on_realtime_transcription_stabilized=lambda text: (
                None if AI_ACTIVE.is_set() and not barge_in_active else emit_text("realtime_stabilized", text)
            ),
            on_recorded_chunk=on_recorded_chunk,
            spinner=False,
            level=40,
            no_log_file=True,
        )
    except Exception as exc:
        recorder_ready.set()
        broadcast({"type": "error", "message": f"Failed to start STT: {exc}"})
        return

    recorder_ready.set()
    broadcast({"type": "listening"})
    threading.Thread(target=watch_raw_audio, daemon=True).start()

    try:
        while not STOP_EVENT.is_set():
            recorder.text(on_final)
    finally:
        try:
            recorder.shutdown()
        except Exception:
            pass


def shutdown(_signum=None, _frame=None) -> None:
    STOP_EVENT.set()


def main() -> int:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()

    tts_worker_thread = threading.Thread(target=_tts_worker, daemon=True)
    tts_worker_thread.start()

    stt_thread = threading.Thread(target=run_stt, daemon=True)
    stt_thread.start()

    while not STOP_EVENT.is_set():
        time.sleep(0.2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
