"""
backend/api.py
---------------
FastAPI application connecting the web frontend to the Python agent.
Run with: uvicorn backend.api:app --reload --port 8000

This is a single-user/single-project tool -- there are no accounts or login
sessions. If multiple people need to use it without stepping on each
other's state (an in-progress recording or plan), each person should run
their own instance (a different port, or their own machine) rather than
share a single process.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import threading
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from database.db import (save_plan, save_session, load_active_plan,
                          get_history, get_statistics,
                          get_session_by_id, rename_session,
                          delete_session, get_plan_by_id)
from browser_agent.agent import is_url_safe

app = FastAPI(title="HearVision AI API")

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS or ["http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend_web")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")


# --- Security headers + payload size limit ------------------------------------
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)
_MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(40 * 1024 * 1024)))  # 40MB


@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Payload too large"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    return response


# --- Simple rate limiting for TTS (costs real money per call) -----------------
_rate_buckets: dict = {}


def _rate_limit(bucket: str, max_n: int, window_sec: int) -> bool:
    now = time.time()
    events = [t for t in _rate_buckets.get(bucket, []) if now - t < window_sec]
    if len(events) >= max_n:
        return False
    events.append(now)
    _rate_buckets[bucket] = events
    return True


# --- In-memory state -- single user, no accounts or sessions ------------------
state = {
    "recorder": None, "session": None, "analysis": None, "plan": None,
    "plan_id": None, "portal_url": "", "email": "", "start_time": None,
    "running": False,
}


# --- Request models -------------------------------------------------------------
class StartRecordingRequest(BaseModel):
    portal_url: str
    email: Optional[str] = ""

class CompletePlanRequest(BaseModel):
    answers: dict

class RunAgentRequest(BaseModel):
    plan: dict
    credentials: dict
    email: Optional[str] = ""


# --- Frontend --------------------------------------------------------------------
@app.get("/")
def root():
    return FileResponse(os.path.join(frontend_path, "index.html"))


# --- Recording ---------------------------------------------------------------------
# Two nearly-simultaneous requests to start a recording (e.g. a permissions
# dialog delayed the first click and the user clicked again) could both pass
# the "if state['recorder']" check before either finished writing to state,
# resulting in two active recorders: two global mouse/keyboard listeners and
# two screen-capture threads running at once. A lock makes the second
# request wait for the first to finish replacing the previous recorder,
# instead of running in parallel.
_recorder_lock = threading.Lock()


@app.post("/recording/start")
def start_recording(req: StartRecordingRequest):
    safe, reason = is_url_safe(req.portal_url)
    if not safe:
        return JSONResponse({"error": f"URL not allowed: {reason}"}, status_code=400)

    from browser_agent.recorder import Recorder
    with _recorder_lock:
        if state["recorder"]:
            try:
                state["recorder"].stop()
            except Exception as e:
                print(f"  Warning: error stopping previous recorder: {e}")

        r = Recorder()
        r.start()
        state["recorder"] = r
        state["portal_url"] = req.portal_url
        state["email"] = req.email
        state["session"] = None
        state["analysis"] = None
        state["plan"] = None
    return {"ok": True}


@app.post("/recording/pause")
def pause_recording():
    r = state.get("recorder")
    if not r:
        return {"ok": False, "error": "No active recording"}
    try:
        r.mouse_listener.stop()
        r.keyboard_listener.stop()
    except Exception as e:
        print(f"  Warning: error pausing listeners: {e}")
    return {"ok": True}


@app.post("/recording/resume")
def resume_recording():
    r = state.get("recorder")
    if not r:
        return {"ok": False, "error": "No active recording"}
    from pynput import mouse, keyboard
    r.mouse_listener = mouse.Listener(on_click=r.on_click)
    r.keyboard_listener = keyboard.Listener(on_press=r.on_key)
    r.mouse_listener.start()
    r.keyboard_listener.start()
    return {"ok": True}


@app.post("/recording/reset")
def reset_recording(req: StartRecordingRequest):
    safe, reason = is_url_safe(req.portal_url)
    if not safe:
        return JSONResponse({"error": f"URL not allowed: {reason}"}, status_code=400)

    from browser_agent.recorder import Recorder
    with _recorder_lock:
        if state["recorder"]:
            try:
                state["recorder"].stop()
            except Exception as e:
                print(f"  Warning: error stopping previous recorder: {e}")
        r = Recorder()
        r.start()
        state["recorder"] = r
        state["portal_url"] = req.portal_url
        state["email"] = req.email
    return {"ok": True}


@app.post("/recording/stop")
def stop_recording():
    r = state.get("recorder")
    if not r:
        return {"error": "No active recording"}

    try:
        session_data = r.stop()
        state["session"] = session_data
        state["recorder"] = None
    except Exception as e:
        return {"error": f"Error stopping the recording: {e}"}

    if len(session_data.get("events", [])) == 0:
        return {"error": "No events were recorded. Please try again."}

    try:
        from core.processor import analyze_session
        result = analyze_session(session_data["events"], session_data["audio_path"])
        result["plan"]["portal_url"] = state["portal_url"]
        state["analysis"] = result
        return result
    except Exception as e:
        return {"error": f"Error analyzing the session: {e}"}


# --- Complete plan ---------------------------------------------------------------
@app.post("/plan/complete")
def complete_plan_endpoint(req: CompletePlanRequest):
    analysis = state.get("analysis")
    if not analysis:
        return JSONResponse({"error": "No analyzed session available"}, status_code=400)
    try:
        from core.processor import complete_plan
        plan, warnings = complete_plan(analysis, req.answers)
        state["plan"] = plan
        plan_id = save_plan(plan)
        state["plan_id"] = plan_id
        state["start_time"] = time.time()
        return plan
    except Exception as e:
        # Covers the fail-closed case: if HEARVISION_ENC_KEY is missing,
        # complete_plan() raises instead of storing credentials unencrypted.
        return JSONResponse({"error": f"Error completing the plan: {e}"}, status_code=500)


# --- Run agent ---------------------------------------------------------------------
@app.post("/agent/run")
async def run_agent_endpoint(req: RunAgentRequest):
    if state.get("running"):
        return JSONResponse({"error": "A run is already in progress"}, status_code=409)

    state["running"] = True
    try:
        from browser_agent.agent import run
        results = await run(req.plan, req.credentials, req.email or "")
        duration = time.time() - (state.get("start_time") or time.time())
        save_session(
            plan=req.plan,
            results=results,
            email=req.email or state.get("email", ""),
            duration_sec=round(duration, 2),
            plan_id=state.get("plan_id"),
        )
        return {"results": results, "ok": True}
    except Exception as e:
        print(f"  Error in /agent/run: {e}")
        return JSONResponse({"error": str(e), "results": [], "ok": False}, status_code=500)
    finally:
        state["running"] = False


VOICE_ID = "9cySrnzVAcRAUGO8JQtx"

# --- Voice (ElevenLabs TTS) -------------------------------------------------------
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = VOICE_ID

@app.get("/tts/available")
def tts_available():
    return {"available": bool(os.getenv("ELEVENLABS_API_KEY"))}

@app.post("/tts")
async def tts(req: TTSRequest, request: Request):
    if not _rate_limit(f"tts:{request.client.host if request.client else '?'}", max_n=60, window_sec=3600):
        return JSONResponse({"error": "Too many voice requests -- try again later"}, status_code=429)
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        return JSONResponse({"error": "ELEVENLABS_API_KEY is not configured"}, status_code=503)
    text = (req.text or "").strip()[:2500]
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)

    voice = req.voice or VOICE_ID
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "output_format": "mp3_44100_128",
    }
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=45) as cx:
            r = await cx.post(url, json=payload, headers=headers)
        if r.status_code == 401:
            return JSONResponse({"error": "ElevenLabs: invalid API key"}, status_code=401)
        if r.status_code != 200:
            return JSONResponse(
                {"error": f"ElevenLabs {r.status_code}: {r.text[:300]}"}, status_code=502)
        return Response(content=r.content, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except httpx.TimeoutException:
        return JSONResponse({"error": "ElevenLabs timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# --- Health check --------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "recording": state["recorder"] is not None}


# --- Local history and statistics -----------------------------------------------
@app.get("/history")
def get_history_endpoint(limit: int = 20):
    return get_history(limit)


class RenameRequest(BaseModel):
    name: str

@app.patch("/history/{session_id}")
def rename_history_endpoint(session_id: int, req: RenameRequest):
    if not req.name.strip():
        return JSONResponse({"error": "Name cannot be empty"}, status_code=400)
    ok = rename_session(session_id, req.name)
    if not ok:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return {"ok": True}


@app.delete("/history/{session_id}")
def delete_history_endpoint(session_id: int):
    ok = delete_session(session_id)
    if not ok:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return {"ok": True}


@app.get("/history/{session_id}/plan")
def get_history_plan_endpoint(session_id: int):
    """Returns the exact plan used in that session -- so it can be re-run
    as-is, even if that portal has since been re-recorded and a newer plan
    version now exists."""
    session = get_session_by_id(session_id)
    if not session or not session.get("plan_id"):
        return JSONResponse({"error": "This session has no associated plan to re-run"}, status_code=404)
    plan = get_plan_by_id(session["plan_id"])
    if not plan:
        return JSONResponse({"error": "The plan for that session is no longer available"}, status_code=404)
    return plan


@app.get("/statistics")
def get_statistics_endpoint():
    return get_statistics()


@app.get("/plan/{portal_url:path}")
def get_active_plan_endpoint(portal_url: str):
    plan = load_active_plan(portal_url)
    if not plan:
        return JSONResponse({"error": "No saved plan for this portal"}, status_code=404)
    return plan
