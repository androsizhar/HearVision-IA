"""
browser_agent/recorder.py
--------------------------
Captures the user's screen, voice, and input events while they demonstrate
a process, so it can later be analyzed and replayed.
"""
import base64
import json
import os
import platform
import queue
import threading
import time
import wave
from datetime import datetime
from io import BytesIO
from pathlib import Path

import mss
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

try:
    from pynput import mouse, keyboard
    PYNPUT_OK = True
except Exception:
    PYNPUT_OK = False
    print("Warning: pynput is not available -- click recording disabled")

try:
    import pyaudio
    PYAUDIO_OK = True
except Exception:
    PYAUDIO_OK = False
    print("Warning: pyaudio is not available -- audio recording disabled")


class Recorder:
    def __init__(self):
        self.events = []
        self.recording = False
        self.folder = Path("sessions")
        self.folder.mkdir(exist_ok=True)
        self.audio_frames = []
        self.audio_stream = None
        self.audio_thread = None
        self._platform = platform.system()  # Windows, Darwin, Linux

        if PYAUDIO_OK:
            self.audio = pyaudio.PyAudio()
        else:
            self.audio = None

        # Screenshot capture is handled by a single dedicated worker thread
        # that owns one long-lived mss instance for the whole recording.
        # Click events only enqueue their coordinates; the worker performs
        # the actual capture serially. This avoids taking screenshots from
        # the input-listener thread, which is unsafe on some platforms.
        self._capture_queue = None
        self._capture_thread = None

    def _capture_worker(self):
        sct = mss.mss()
        try:
            while True:
                item = self._capture_queue.get()
                if item is None:  # shutdown signal
                    break
                x, y, ts = item
                screenshot = None
                try:
                    monitor = sct.monitors[1]
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    img.thumbnail((1280, 720))
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=70)
                    screenshot = base64.b64encode(buf.getvalue()).decode()
                except Exception as e:
                    print(f"  Warning: error capturing screenshot: {e}")
                self.events.append({
                    "type": "click", "x": x, "y": y,
                    "timestamp": ts, "screenshot": screenshot,
                })
                print(f"  click ({x:.0f}, {y:.0f}) -- {len(self.events)} events")
        finally:
            try:
                sct.close()
            except Exception:
                pass

    def capture_screenshot(self) -> str:
        """Kept for one-off use outside of a live recording (e.g. manual
        testing) -- during an actual recording, the real flow is
        on_click() -> queue -> _capture_worker()."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            img.thumbnail((1280, 720))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return base64.b64encode(buf.getvalue()).decode()

    def on_click(self, x, y, button, pressed):
        if not self.recording or not pressed:
            return
        # Only enqueue here -- the actual capture happens in
        # _capture_worker(), on its own dedicated thread.
        self._capture_queue.put((x, y, datetime.now().isoformat()))

    def on_key(self, key):
        if not self.recording:
            return
        try:
            char = key.char
        except AttributeError:
            char = str(key)
        self.events.append({
            "type": "key",
            "key": char,
            "timestamp": datetime.now().isoformat()
        })

    def record_audio(self):
        if not PYAUDIO_OK:
            return
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        try:
            self.audio_stream = self.audio.open(
                format=FORMAT, channels=CHANNELS,
                rate=RATE, input=True,
                frames_per_buffer=CHUNK
            )
            while self.recording:
                data = self.audio_stream.read(CHUNK, exception_on_overflow=False)
                self.audio_frames.append(data)
        except Exception as e:
            print(f"  Warning: audio error: {e}")

    def start(self):
        self.events = []
        self.audio_frames = []
        self.recording = True

        self._capture_queue = queue.Queue()
        self._capture_thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._capture_thread.start()

        if PYAUDIO_OK:
            self.audio_thread = threading.Thread(target=self.record_audio, daemon=True)
            self.audio_thread.start()

        if PYNPUT_OK:
            self.mouse_listener = mouse.Listener(on_click=self.on_click)
            self.keyboard_listener = keyboard.Listener(on_press=self.on_key)
            self.mouse_listener.start()
            self.keyboard_listener.start()

        print("RECORDING -- speak and perform the process now")
        print("   Press ENTER when done\n")

    def stop(self) -> dict:
        self.recording = False

        if PYNPUT_OK:
            try:
                self.mouse_listener.stop()
                self.keyboard_listener.stop()
            except Exception:
                pass

        # Wait for the audio thread to exit its current blocking read()
        # before touching the stream from this other thread. Closing a
        # PyAudio stream while another thread is still reading from it is a
        # common cause of a hard native crash.
        if self.audio_thread:
            try:
                self.audio_thread.join(timeout=5)
            except Exception:
                pass

        # Wait for the capture worker to finish processing anything already
        # queued before reading self.events.
        if self._capture_thread:
            try:
                self._capture_queue.put(None)
                self._capture_thread.join(timeout=5)
            except Exception:
                pass
            finally:
                self._capture_thread = None

        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except Exception:
                pass
            finally:
                self.audio_stream = None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_path = self.folder / f"audio_{ts}.wav"

        if PYAUDIO_OK and self.audio_frames:
            with wave.open(str(audio_path), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(self.audio.get_sample_size(pyaudio.paInt16))
                wf.setframerate(16000)
                wf.writeframes(b''.join(self.audio_frames))
        else:
            # Write an empty WAV file if no audio was captured.
            with wave.open(str(audio_path), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b'')

        log_path = self.folder / f"events_{ts}.json"
        lightweight = [{k: v for k, v in e.items() if k != "screenshot"} for e in self.events]
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(lightweight, f, indent=2, ensure_ascii=False)

        print(f"\nStopped: {len(self.events)} events, audio saved")
        print(f"   Audio: {audio_path}")

        return {
            "events": self.events,
            "audio_path": str(audio_path),
            "timestamp": ts
        }


if __name__ == "__main__":
    r = Recorder()
    r.start()
    input()
    result = r.stop()
    print(f"\nSession saved with {len(result['events'])} events")
