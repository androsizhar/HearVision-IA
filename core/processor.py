"""
core/processor.py -- used by backend/api.py
--------------------------------------------
Phase A: analyze_session  -> screenshots + audio -> plan + questions for the frontend
Phase B: complete_plan    -> receives answers from the UI -> finalizes the plan
"""
import json
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_api_key = os.getenv("ANTHROPIC_API_KEY")
if not _api_key:
    raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")
client = Anthropic(api_key=_api_key)


# --- Utility: robust JSON parsing for Claude's responses ---------------------

def _parse_json(raw: str) -> dict:
    """
    Extracts the first complete JSON object from a Claude response.
    Balances braces (supports nested JSON) and ignores surrounding text,
    code fences, or comments.
    """
    if not raw:
        return {}
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = raw.find("{", start + 1)
    return {}


# --- Audio ---------------------------------------------------------------------

def transcribe_audio(audio_path: str) -> str:
    if not audio_path or not Path(audio_path).exists():
        return ""
    try:
        from groq import Groq
        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        with open(audio_path, "rb") as f:
            transcript = groq_client.audio.transcriptions.create(
                model="whisper-large-v3", file=f,
            )
        return transcript.text.strip()
    except Exception as e:
        print(f"  Warning: Groq transcription unavailable: {e}")
        return ""


# --- Phase A ---------------------------------------------------------------------

def analyze_session(events: list, audio_path: str) -> dict:
    """
    Returns {"plan": {...}, "questions": [...], "already_known": [...]}
    for the frontend to display. Never blocks on user input.
    """
    transcript = transcribe_audio(audio_path)

    with_screenshot = [e for e in events if e.get("screenshot")]
    if not with_screenshot:
        raise ValueError("No screenshots found in the recording. Did it record correctly?")

    stride = max(1, len(with_screenshot) // 10)
    keyframes = with_screenshot[::stride][:10]

    # -- Step 1: Claude analyzes screenshots + audio and generates the plan --
    content = []
    for i, event in enumerate(keyframes):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": event["screenshot"]},
        })
        content.append({
            "type": "text",
            "text": f"Moment {i+1}: {event['type']} at ({event.get('x', '')}, {event.get('y', '')})",
        })

    audio_context = (
        f'\nThe user explained while working:\n"{transcript}"\n'
        if transcript
        else "\n(No audio -- infer the process from the images alone)\n"
    )

    content.append({"type": "text", "text": f"""{audio_context}
Analyze everything and generate the plan.

IMPORTANT -- do not assume the process always involves two distinct systems:
- If the user moved data from one place to another (e.g. from a spreadsheet
  to a web portal), then there is genuinely a SOURCE (where the data comes
  from) and a TARGET (where it gets recorded).
- But if the whole process happened within a single system (e.g. searching
  for and playing a video on YouTube, filling out a form on one site), do
  NOT invent a second system -- use the same name for both "source_platform"
  and "target_platform", or leave "target_platform" empty.
- HearVision AI (this very tool, the one currently recording) is NEVER the
  source or target system of the process -- it is the app doing the
  recording and execution, not part of the workflow itself. If the user
  returns to this app to stop the recording, or mentions "HearVision" while
  narrating what they're doing (e.g. "to test HearVision AI"), that is
  context about the tool, not a system to navigate to. Ignore it when
  defining source/target and when generating steps.

When there genuinely are two systems, fields may have DIFFERENT names in
each one -- learn the real mapping.
NEVER use CSS selectors or IDs. Describe elements visually.
In "required_credentials" include ONLY data the agent cannot see on screen
(passwords, tokens, fields shown masked, or anything the user typed that the
camera did not capture).

Respond with ONLY this JSON, no extra text:
{{
  "source_platform": "name of the source system",
  "target_platform": "name of the target system",
  "goal": "what this process accomplishes",
  "field_mappings": [
    {{
      "source_field": "name in the source system",
      "target_field": "name in the target system",
      "description": "what this field represents",
      "confidence": 0.95
    }}
  ],
  "required_credentials": ["one item per credential, e.g. 'hearvision_username', 'hearvision_password'"],
  "steps": [
    {{
      "number": 1,
      "system": "source or target",
      "intent": "visual description of what to do",
      "action": "navigate|click|type|select|verify|wait|extract",
      "value": "URL or text, if applicable",
      "validation": "how to know it worked"
    }}
  ],
  "exceptions": [{{"situation": "what could go wrong", "action": "what to do"}}],
  "report_fields": ["data to include in the report"]
}}"""})

    plan_response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
    )

    plan = _parse_json(plan_response.content[0].text)
    if not plan:
        raise ValueError("Claude did not return a valid JSON plan")

    # Safety net: even though the prompt already instructs this, a model can
    # still get it wrong. If it names this tool itself as the source/target
    # system anyway, correct it here rather than trusting the instruction
    # alone.
    for field in ("source_platform", "target_platform"):
        value = str(plan.get(field, "")).lower()
        if any(p in value for p in _APP_SELF_REFERENCE_PATTERNS):
            plan[field] = plan.get("source_platform", "") if field == "target_platform" else ""

    # -- Step 2: figure out what still needs to be asked --
    analysis = generate_questions(plan, transcript)

    return {
        "plan": plan,
        "questions": analysis["questions"],
        "already_known": analysis.get("already_known", []),
    }


def generate_questions(initial_plan: dict, transcript: str) -> dict:
    """
    Identifies what information is genuinely missing to execute the process.
    Returns {"questions": [...], "already_known": [...]} -- never blocks on
    user input. Each question covers exactly one piece of data (username and
    password are never combined into a single question).
    """
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": f"""You are an intelligent agent that learned a web process by observing a user.

Generated plan:
{json.dumps(initial_plan, indent=2, ensure_ascii=False)}

The user explained: "{transcript or '(no audio)'}"

Identify what information you genuinely need to execute this process without
stalling halfway through. The worst case is the agent reaching a form and not
knowing what to put in a field -- that traps it in a type-and-delete loop.
Better to ask now than get stuck later.

Equally important: do NOT ask for credentials, a username, or a password if
the recording did not show an actual login (a masked password field, a
sign-in form). If the recorded process never required signing in, don't
assume it does -- that produces pointless questions and wastes the time of
whoever is using the tool.

CREDENTIALS: ask ONLY about what is already listed in "required_credentials"
in the plan above -- that list was already generated by requiring real
evidence (a masked password field, a field the camera could not read). Do
not add extra credentials "just in case", and do not assume a system
requires login just because it's a well-known service.

Beyond that, check these categories ONLY if they genuinely apply to the
observed process -- they are common examples, not a mandatory checklist; the
process may not need any of them:

- IDENTIFICATION DATA (if the form asks to identify a person or organization):
   customer/account ID in the target system, tax ID, company name, contact name
- DELIVERY DATA (if the process involves physically shipping something):
   full address, postal code, contact phone number
- TRANSACTION OR ORDER DATA (if the process records a purchase, order, or
  formal request): order/reference number if one already exists, deadline,
  payment terms, cost center

RULES:
- Do NOT ask about buttons, menus, or navigation
- Do NOT ask about data already visible in the screenshots
- DO ask about any text field the user filled in that isn't obvious
- Each question covers exactly one piece of data
- Generate one question per item in the plan's "required_credentials" -- no more, no less
- For the categories above, only include a question if there is concrete evidence in the plan/screenshots that it applies
- Maximum 10 questions total -- priority order: credentials > identification > transaction data > delivery
- Set is_password: true for passwords, tokens, and secret keys

Respond with ONLY JSON:
{{
  "questions": [
    {{
      "field": "internal_name_no_spaces",
      "question": "Clear, specific question about the detected system",
      "reason": "why this couldn't be inferred from the screenshots",
      "is_password": false
    }}
  ],
  "already_known": ["concrete fact learned without needing to ask"]
}}"""}],
    )

    data = _parse_json(response.content[0].text)
    if not data:
        data = {"questions": [], "already_known": []}
    data.setdefault("questions", [])
    data.setdefault("already_known", [])

    # Fall back to inferring is_password from the field name if Claude omitted it.
    for q in data.get("questions", []):
        if "is_password" not in q:
            q["is_password"] = any(
                w in q.get("field", "").lower()
                for w in ["password", "pass", "token", "secret", "pwd"]
            )

    return data


# --- Recording-artifact filter ------------------------------------------------

_APP_SELF_REFERENCE_PATTERNS = (
    "localhost", "127.0.0.1", "0.0.0.0", "hearvision", ":8000", ":8080",
    "back to the app", "return to the app", "stop recording", "close recording",
)

def _filter_recorder_artifacts(plan: dict) -> None:
    """
    Removes trailing steps in plan["steps"] that correspond to navigating
    back to the HearVision AI interface itself -- an artifact of the user
    clicking "Stop recording", which the recorder also captures as a
    click/navigation event. Only trims from the tail so the actual process
    steps are never touched.
    """
    steps = plan.get("steps", [])
    if not steps:
        return
    remaining_checks = min(3, len(steps))
    trimmed = list(steps)
    while trimmed and remaining_checks > 0:
        last = trimmed[-1]
        text = " ".join([
            str(last.get("value", "")),
            str(last.get("intent", "")),
            str(last.get("validation", "")),
        ]).lower()
        if any(p in text for p in _APP_SELF_REFERENCE_PATTERNS):
            trimmed.pop()
            remaining_checks -= 1
        else:
            break
    plan["steps"] = trimmed


# --- Phase B ---------------------------------------------------------------------

def complete_plan(phase_a_result: dict, user_answers: dict) -> tuple:
    """
    Merges the frontend's answers into the plan.
    Returns (completed_plan, warnings_list).

    Credentials are NEVER stored in the plan in plain text: they are
    encrypted and stored in the `secrets` table (database/db.py), and the
    plan only keeps a secret_id per field. This is fail-closed -- if no
    encryption key is configured (HEARVISION_ENC_KEY), this function raises
    a clear error instead of storing unencrypted credentials.
    """
    plan = phase_a_result.get("plan", phase_a_result)

    warnings = []
    for q in phase_a_result.get("questions", []):
        if not user_answers.get(q["field"]):
            warnings.append(f"Missing answer for: {q['question']}")

    from database.db import save_secret
    credential_refs = {}
    for field, value in user_answers.items():
        if value:
            credential_refs[field] = save_secret(value)
    plan["credential_refs"] = credential_refs

    # Trim trailing steps that are recorder artifacts (the user returning to
    # the HearVision AI app to stop the recording -- not part of the real process).
    _filter_recorder_artifacts(plan)

    Path("sessions").mkdir(exist_ok=True)
    with open("sessions/plan.json", "w", encoding="utf-8") as f:
        # The plan saved to disk also carries no secrets -- only references.
        json.dump(plan, f, indent=2, ensure_ascii=False)

    return plan, warnings
