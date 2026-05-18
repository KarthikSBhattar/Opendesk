import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import "./App.css";

const idleLevels = [
  0.18, 0.32, 0.48, 0.28, 0.2, 0.34, 0.52, 0.36, 0.3, 0.58, 0.18, 0.35,
  0.33, 0.5, 0.38, 0.46,
];

const demoTranscriptWords = [
  "Lorem",
  "ipsum",
  "dolor",
  "sit",
  "amet,",
  "consectetur",
  "adipiscing",
  "elit,",
  "sed",
  "do",
  "eiusmod",
  "tempor",
  "incididunt",
  "ut",
  "labore",
  "et",
  "dolore",
  "magna",
  "aliqua.",
  "Ut",
  "enim",
  "ad",
  "minim",
  "veniam,",
  "quis",
  "nostrud",
  "exercitation",
  "ullamco",
  "laboris",
  "nisi",
  "ut",
  "aliquip",
  "ex",
  "ea",
  "commodo",
  "consequat.",
];

const demoResponseWords = [
  "I",
  "heard",
  "you.",
  "Here",
  "is",
  "a",
  "quick",
  "response",
  "while",
  "Gemma",
  "thinks",
  "through",
  "the",
  "request",
  "and",
  "prepares",
  "the",
  "next",
  "step.",
];

const MAX_TRANSCRIPT_HEIGHT = 68;
const MAX_PLAN_STEP_CHARS = 44;
const STT_EVENTS_URL = "http://127.0.0.1:38476/events";

type SttEvent = {
  type:
    | "bridge_connected"
    | "server_ready"
    | "loading"
    | "listening"
    | "recording_start"
    | "recording_stop"
    | "realtime_update"
    | "realtime_stabilized"
    | "final"
    | "utterance_finalized"
    | "ai_start"
    | "ai_thinking"
    | "ai_token"
    | "ai_done"
    | "audio_levels"
    | "audio_status"
    | "tts_start"
    | "tts_audio"
    | "tts_end"
    | "tts_stop"
    | "agent_start"
    | "agent_step"
    | "agent_action"
    | "agent_thought"
    | "agent_ask"
    | "agent_done"
    | "agent_cancelled"
    | "error";
  text?: string;
  message?: string;
  audio?: string;
  id?: string;
  sampleRate?: number;
  levels?: number[];
  label?: string;
  step?: number;
  result?: string;
  question?: string;
};

function splitWords(text: string) {
  return text.trim().split(/\s+/).filter(Boolean);
}

function truncateMiddle(text: string, maxLength = MAX_PLAN_STEP_CHARS) {
  const normalized = text.trim().replace(/\s+/g, " ");
  if (normalized.length <= maxLength) return normalized;

  const edgeLength = Math.floor((maxLength - 3) / 2);
  const tailLength = maxLength - 3 - edgeLength;
  return `${normalized.slice(0, edgeLength).trimEnd()}...${normalized.slice(-tailLength).trimStart()}`;
}

function microphoneErrorMessage(error: unknown) {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Microphone permission denied";
    }
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
      return "No microphone found";
    }
  }

  return "Microphone unavailable";
}

function pcm16Base64ToAudioBuffer(
  context: AudioContext,
  encodedAudio: string,
  sampleRate: number,
) {
  const binary = atob(encodedAudio);
  const sampleCount = Math.floor(binary.length / 2);
  const buffer = context.createBuffer(1, sampleCount, sampleRate);
  const channel = buffer.getChannelData(0);

  for (let index = 0; index < sampleCount; index += 1) {
    const low = binary.charCodeAt(index * 2);
    const high = binary.charCodeAt(index * 2 + 1);
    const sample = (high << 8) | low;
    channel[index] = (sample >= 0x8000 ? sample - 0x10000 : sample) / 32768;
  }

  return buffer;
}

function TokenArrow({ direction }: { direction: "up" | "down" }) {
  return (
    <svg className="token-arrow" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d={direction === "up" ? "M12 19V5" : "M12 5v14"}
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d={direction === "up" ? "M7 10l5-5 5 5" : "M7 14l5 5 5-5"}
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function App() {
  const [isRecording, setIsRecording] = useState(false);
  const [isThinking, setIsThinking] = useState(false);
  const [isResponseDone, setIsResponseDone] = useState(false);
  const [transcriptText, setTranscriptText] = useState("");
  const [responseText, setResponseText] = useState("");
  const [thinkingText, setThinkingText] = useState("");
  const [planStepText, setPlanStepText] = useState("");
  const [statusText, setStatusText] = useState("Gemma is loading");
  const [isModelLoading, setIsModelLoading] = useState(true);
  const [audioLevels, setAudioLevels] = useState(idleLevels);
  const [demoWordCount, setDemoWordCount] = useState(0);
  const [demoResponseWordCount, setDemoResponseWordCount] = useState(0);
  const [transcriptHeight, setTranscriptHeight] = useState(0);
  const [isTranscriptScrollable, setIsTranscriptScrollable] = useState(false);
  const [isDemoMode, setIsDemoMode] = useState(false);
  const bridgeConnectedRef = useRef(false);
  const isAiActiveRef = useRef(false);
  const micPermissionBlockedRef = useRef(false);
  const audioContextRef = useRef<AudioContext | null>(null);
  const currentTtsIdRef = useRef<string | null>(null);
  const ttsNextStartTimeRef = useRef(0);
  const ttsSourcesRef = useRef<AudioBufferSourceNode[]>([]);
  const transcriptDraftRef = useRef("");
  const transcriptRef = useRef<HTMLDivElement>(null);

  function ensureAudioContext() {
    const AudioContextConstructor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextConstructor) return null;

    if (!audioContextRef.current) {
      audioContextRef.current = new AudioContextConstructor();
    }

    if (audioContextRef.current.state === "suspended") {
      void audioContextRef.current.resume().catch(() => undefined);
    }

    return audioContextRef.current;
  }

  function stopTtsPlayback() {
    for (const source of ttsSourcesRef.current) {
      try {
        source.stop();
      } catch {
        // Source may already have ended.
      }
    }
    ttsSourcesRef.current = [];
    currentTtsIdRef.current = null;
    const context = audioContextRef.current;
    ttsNextStartTimeRef.current = context?.currentTime ?? 0;
  }

  function playTtsChunk(encodedAudio: string, sampleRate = 24000) {
    const context = ensureAudioContext();
    if (!context) return;

    const buffer = pcm16Base64ToAudioBuffer(context, encodedAudio, sampleRate);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);

    const startTime = Math.max(context.currentTime, ttsNextStartTimeRef.current);
    source.start(startTime);
    ttsNextStartTimeRef.current = startTime + buffer.duration;
    ttsSourcesRef.current.push(source);
    source.onended = () => {
      ttsSourcesRef.current = ttsSourcesRef.current.filter((node) => node !== source);
    };
  }

  useEffect(() => {
    let isCancelled = false;

    async function requestMicrophoneAccess() {
      if (!navigator.mediaDevices?.getUserMedia) return;

      try {
        setStatusText("Checking microphone access");
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((track) => track.stop());
        if (!isCancelled) {
          micPermissionBlockedRef.current = false;
        }
      } catch (error) {
        if (!isCancelled) {
          micPermissionBlockedRef.current = true;
          setIsModelLoading(false);
          setStatusText(microphoneErrorMessage(error));
        }
      }
    }

    void requestMicrophoneAccess();

    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    const source = new EventSource(STT_EVENTS_URL);
    const fallbackTimer = window.setTimeout(() => {
      if (!bridgeConnectedRef.current) setIsDemoMode(true);
    }, 8000);

    source.onmessage = (message) => {
      bridgeConnectedRef.current = true;
      window.clearTimeout(fallbackTimer);

      const event = JSON.parse(message.data) as SttEvent;

      if (event.type === "bridge_connected") {
        setIsDemoMode(false);
        setStatusText("Gemma is loading");
        setIsModelLoading(true);
        return;
      }

      if (event.type === "error") {
        console.error(event.message);
        isAiActiveRef.current = false;
        setIsThinking(false);
        setStatusText(event.message ?? "Speech recognition unavailable");
        setIsModelLoading(false);
        return;
      }

      if (event.type === "loading") {
        if (!micPermissionBlockedRef.current) {
          setStatusText(event.message ?? "Gemma is loading");
          setIsModelLoading(true);
        }
        return;
      }

      if (event.type === "listening") {
        if (micPermissionBlockedRef.current) return;
        setStatusText("Gemma is listening");
        setIsModelLoading(false);
        return;
      }

      if (event.type === "recording_start") {
        isAiActiveRef.current = false;
        setIsRecording(true);
        setIsThinking(false);
        setIsResponseDone(false);
        setResponseText("");
        setThinkingText("");
        setPlanStepText("");
        setAudioLevels(idleLevels);
        setTranscriptText("");
        transcriptDraftRef.current = "";
        return;
      }

      if (event.type === "recording_stop") {
        setIsRecording(false);
        setAudioLevels(idleLevels);
        if (transcriptDraftRef.current.trim()) {
          setIsThinking(true);
        }
        return;
      }

      if (event.type === "audio_levels" && event.levels?.length) {
        micPermissionBlockedRef.current = false;
        if (isAiActiveRef.current) return;
        setAudioLevels((currentLevels) =>
          idleLevels.map((_, index) => {
            const nextLevel = event.levels?.[index] ?? 0;
            const currentLevel = currentLevels[index] ?? 0;
            return currentLevel * 0.35 + nextLevel * 0.65;
          }),
        );
        return;
      }

      if (event.type === "audio_status") {
        if (isAiActiveRef.current) return;
        setStatusText(event.message ?? "Checking microphone signal");
        setIsModelLoading(false);
        return;
      }

      if (event.type === "tts_start") {
        stopTtsPlayback();
        currentTtsIdRef.current = event.id ?? null;
        return;
      }

      if (event.type === "tts_audio" && event.audio) {
        if (currentTtsIdRef.current && event.id && event.id !== currentTtsIdRef.current) {
          return;
        }
        if (!currentTtsIdRef.current) currentTtsIdRef.current = event.id ?? null;
        playTtsChunk(event.audio, event.sampleRate);
        return;
      }

      if (event.type === "tts_end") {
        if (!event.id || event.id === currentTtsIdRef.current) {
          currentTtsIdRef.current = null;
        }
        return;
      }

      if (event.type === "tts_stop") {
        stopTtsPlayback();
        return;
      }

      if (
        event.type === "realtime_update" ||
        event.type === "realtime_stabilized"
      ) {
        micPermissionBlockedRef.current = false;
        if (isAiActiveRef.current) return;
        setIsRecording(true);
        setIsThinking(false);
        setResponseText("");
        transcriptDraftRef.current = event.text ?? "";
        setTranscriptText(event.text ?? "");
        return;
      }

      if (event.type === "final") {
        micPermissionBlockedRef.current = false;
        if (isAiActiveRef.current) return;
        setIsRecording(false);
        setAudioLevels(idleLevels);
        transcriptDraftRef.current = event.text ?? "";
        setTranscriptText(event.text ?? "");
        setIsThinking(true);
        return;
      }

      if (event.type === "utterance_finalized") {
        if (isAiActiveRef.current) return;
        setIsRecording(false);
        setAudioLevels(idleLevels);
        if (event.text) {
          transcriptDraftRef.current = event.text;
          setTranscriptText(event.text);
        }
        setIsThinking(true);
        return;
      }

      if (event.type === "ai_start") {
        isAiActiveRef.current = true;
        setIsThinking(true);
        setIsRecording(false);
        setResponseText("");
        setThinkingText("");
        setPlanStepText("");
        setStatusText("Gemma is thinking");
        return;
      }

      if (event.type === "ai_thinking") {
        if (!isAiActiveRef.current) return;
        setIsRecording(false);
        setThinkingText((prev) => prev + (event.text ?? ""));
        return;
      }

      if (event.type === "ai_token") {
        if (!isAiActiveRef.current) return;
        setIsRecording(false);
        setThinkingText("");
        setResponseText((prev) => prev + (event.text ?? ""));
        return;
      }

      if (event.type === "ai_done") {
        if (!isAiActiveRef.current) return;
        isAiActiveRef.current = false;
        setIsThinking(false);
        setIsRecording(false);
        setIsResponseDone(true);
        setStatusText("Gemma is listening");
        return;
      }

      if (event.type === "agent_start") {
        isAiActiveRef.current = true;
        setIsThinking(true);
        setIsRecording(false);
        setResponseText("");
        setThinkingText("");
        setPlanStepText("");
        setStatusText("Using your computer");
        return;
      }

      if (event.type === "agent_step") {
        setIsRecording(false);
        setPlanStepText(event.label ?? `Step ${event.step ?? "?"}`);
        setStatusText("Using your computer");
        return;
      }

      if (event.type === "agent_action") {
        setIsRecording(false);
        setResponseText(event.label ?? "Acting…");
        return;
      }

      if (event.type === "agent_thought") {
        if (!isAiActiveRef.current) return;
        setIsRecording(false);
        setResponseText(event.text ?? "");
        return;
      }

      if (event.type === "agent_done") {
        isAiActiveRef.current = false;
        setIsThinking(false);
        setIsRecording(false);
        setIsResponseDone(true);
        setPlanStepText("");
        setStatusText("Gemma is listening");
        if (event.result) setResponseText(event.result);
        return;
      }

      if (event.type === "agent_ask") {
        isAiActiveRef.current = false;
        setIsThinking(false);
        setIsRecording(false);
        setIsResponseDone(true);
        setPlanStepText("");
        setStatusText("Needs your answer");
        setResponseText(event.question ?? event.result ?? "I need a little more detail.");
        return;
      }

      if (event.type === "agent_cancelled") {
        isAiActiveRef.current = false;
        setIsThinking(false);
        setIsRecording(false);
        setPlanStepText("");
        setStatusText("Gemma is listening");
        return;
      }
    };

    source.onerror = () => {
      isAiActiveRef.current = false;
      setIsThinking(false);
      setIsRecording(false);
      setPlanStepText("");
      if (bridgeConnectedRef.current) {
        setIsModelLoading(false);
        setStatusText("Speech bridge disconnected");
      }
    };

    return () => {
      window.clearTimeout(fallbackTimer);
      source.close();
      stopTtsPlayback();
    };
  }, []);

  useEffect(() => {
    if (!isDemoMode) return;

    setIsModelLoading(false);
    setStatusText("Gemma is listening");
    const recordingTimer = window.setTimeout(() => setIsRecording(true), 800);
    const wordTimer = window.setInterval(() => {
      setDemoWordCount((count) =>
        count >= demoTranscriptWords.length ? count : count + 1,
      );
    }, 165);
    const levelTimer = window.setInterval(() => {
      setAudioLevels(
        idleLevels.map((_, index) => {
          const phase = Date.now() / (180 + index * 9) + index * 0.77;
          return 0.18 + Math.abs(Math.sin(phase)) * 0.78;
        }),
      );
    }, 55);

    return () => {
      window.clearTimeout(recordingTimer);
      window.clearInterval(wordTimer);
      window.clearInterval(levelTimer);
    };
  }, [isDemoMode]);

  useEffect(() => {
    if (!isThinking) return;

    const wordTimer = isDemoMode ? window.setInterval(() => {
      setDemoResponseWordCount((count) =>
        count >= demoResponseWords.length ? count : count + 1,
      );
    }, 120) : null;
    const levelTimer = window.setInterval(() => {
      setAudioLevels(
        idleLevels.map((_, index) => {
          const phase = Date.now() / (240 + index * 14) + index * 0.61;
          return 0.16 + Math.abs(Math.sin(phase)) * 0.64;
        }),
      );
    }, 70);

    return () => {
      if (wordTimer) window.clearInterval(wordTimer);
      window.clearInterval(levelTimer);
    };
  }, [isThinking, isDemoMode]);

  const activeResponseText = responseText || thinkingText;
  const isShowingThinking = isThinking && !responseText && !!thinkingText;
  const transcriptWords = (isThinking || isResponseDone)
    ? isDemoMode
      ? demoResponseWords.slice(0, demoResponseWordCount)
      : splitWords(activeResponseText)
    : transcriptText
      ? splitWords(transcriptText)
      : demoTranscriptWords.slice(0, demoWordCount);
  const transcriptSourceWords = transcriptText
    ? splitWords(transcriptText)
    : demoTranscriptWords.slice(0, demoWordCount);
  const uploadedTokenCount = transcriptSourceWords.length;
  const downloadedTokenCount = isDemoMode
    ? demoResponseWordCount
    : splitWords(responseText).length;

  const showPlanStep = isThinking && !!planStepText;
  const planStepPreview = truncateMiddle(planStepText);
  const showRecordingText = transcriptWords.length > 0 || showPlanStep || isThinking || isResponseDone;

  useLayoutEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;

    const nextHeight = Math.min(transcript.scrollHeight, MAX_TRANSCRIPT_HEIGHT);
    setTranscriptHeight(nextHeight);
    setIsTranscriptScrollable(transcript.scrollHeight > MAX_TRANSCRIPT_HEIGHT);

    window.requestAnimationFrame(() => {
      transcript.scrollTo({
        top: transcript.scrollHeight,
        behavior: "smooth",
      });
    });
  }, [transcriptWords.length, transcriptText, activeResponseText, planStepText]);

  const pillHeight =
    showRecordingText && transcriptHeight > 0 ? 42 + transcriptHeight : undefined;
  const tokenCount = (isThinking || isResponseDone) ? downloadedTokenCount : uploadedTokenCount;
  const tokenDirection = (isThinking || isResponseDone) ? "down" : "up";
  const handleWindowDrag = (event: MouseEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    if ((event.target as HTMLElement).closest("button")) return;

    void getCurrentWindow().startDragging();
  };

  return (
    <main className="window-shell">
      <section
        className={`island-pill ${isModelLoading ? "is-model-loading" : "is-model-ready"} ${
          (isThinking || isResponseDone) ? "is-thinking" : isRecording ? "is-recording" : "is-listening"
        } ${
          showRecordingText ? "is-expanded" : ""
        } ${isTranscriptScrollable ? "has-transcript-overflow" : ""}`}
        style={pillHeight ? { height: `${pillHeight}px` } : undefined}
        onMouseDown={handleWindowDrag}
      >
        <div className="listening-state">
          <div className="listening-orb" data-tauri-drag-region />
          <div className="listening-copy" data-tauri-drag-region>
            {statusText}
          </div>
          <div className="right-control-stack">
            <div
              className="loading-control"
              aria-label="Loading speech model"
              data-tauri-drag-region
            >
              <svg className="loading-control-ring" viewBox="0 0 50 50">
                <circle
                  className="loading-control-track"
                  strokeWidth="6"
                  stroke="currentColor"
                  fill="transparent"
                  r="22"
                  cx="25"
                  cy="25"
                />
                <circle
                  className="loading-control-progress"
                  strokeLinecap="round"
                  strokeWidth="6"
                  stroke="currentColor"
                  fill="transparent"
                  r="22"
                  cx="25"
                  cy="25"
                  pathLength="1"
                />
              </svg>
              <div className="loading-control-stop" />
            </div>
            <button
              className="exit-button"
              type="button"
              aria-label="Exit"
              onClick={() => getCurrentWindow().close()}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <path
                  fill="currentColor"
                  d="M6.4 5.2 12 10.8l5.6-5.6a.85.85 0 1 1 1.2 1.2L13.2 12l5.6 5.6a.85.85 0 0 1-1.2 1.2L12 13.2l-5.6 5.6a.85.85 0 0 1-1.2-1.2l5.6-5.6-5.6-5.6a.85.85 0 1 1 1.2-1.2Z"
                />
              </svg>
            </button>
          </div>
        </div>

        <div className="recording-state" data-tauri-drag-region>
          <div className="recording-main" data-tauri-drag-region>
            <div className="meter" data-tauri-drag-region>
              {audioLevels.map((level, index) => (
                <div
                  className="meter-bar"
                  key={index}
                  style={{
                    height: `${4 + level * 13}px`,
                  }}
                  data-tauri-drag-region
                />
              ))}
            </div>
            <div className="token-stat" data-tauri-drag-region>
              <TokenArrow direction={tokenDirection} />
              <span className="token-stat-count">{tokenCount}</span>
              <span className="token-stat-label">T</span>
            </div>
          </div>
          <div
            className="recording-caption"
            data-tauri-drag-region
            ref={transcriptRef}
            style={
              transcriptHeight ? { maxHeight: `${transcriptHeight}px` } : undefined
            }
          >
            {showPlanStep && (
              <div className="plan-step-text" title={planStepText}>{planStepPreview}</div>
            )}
            {transcriptWords.length === 0 && isThinking && !showPlanStep && (
              <span className="transcript-word is-thinking-word is-thinking-placeholder">Thinking…</span>
            )}
            {transcriptWords.map((word, index) => (
              <span className={`transcript-word${isShowingThinking ? " is-thinking-word" : ""}`} key={`${word}-${index}`}>
                {word}
              </span>
            ))}
          </div>
        </div>
      </section>
    </main>
  );
}

export default App;
