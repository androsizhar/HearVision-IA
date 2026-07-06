"""
main.py -- HearVision AI CLI
------------------------------
Run with: python main.py
Web mode: uvicorn backend.api:app --reload --port 8000

Uses the same core (core/processor.py) and the same recorder
(browser_agent/recorder.py) as the web interface, so a plan generated from
the CLI is identical to one generated from the browser.
"""

import asyncio
import getpass
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from browser_agent.recorder import Recorder
from core.processor import analyze_session, complete_plan
from browser_agent.agent import run as run_agent


def ask_in_terminal(phase_a_result: dict) -> dict:
    """
    Adapts the Phase A question flow (designed for the web frontend) into
    an interactive terminal version using input()/getpass().
    """
    if phase_a_result.get("already_known"):
        print("\n  Learned without asking:")
        for fact in phase_a_result["already_known"]:
            print(f"     - {fact}")

    answers = {}
    questions = phase_a_result.get("questions", [])
    if questions:
        print(f"\n  A few things I couldn't see on screen:\n")
        for q in questions:
            print(f"  Why I need it: {q['reason']}")
            if q.get("is_password"):
                value = getpass.getpass(f"  {q['question']}: ")
            else:
                value = input(f"  {q['question']}: ").strip()
            answers[q["field"]] = value
            print()
    else:
        print("\n  Got everything I need -- nothing left to ask")

    return answers


async def main():
    print("\n" + "=" * 55)
    print("  HearVision AI")
    print("  Automate any repetitive process between two systems")
    print("=" * 55)
    print()
    print("  Options:")
    print("  1. Record a new process")
    print("  2. Run a saved process")
    print("  3. View history (SQLite)")
    print()

    choice = input("  Choose (1/2/3): ").strip()

    if choice == "1":
        print("\n  Narrate out loud what you're doing.")
        print("  Perform the process in your browser as usual.")
        print()
        input("  Press ENTER to start recording...")

        r = Recorder()
        r.start()
        input()  # wait for ENTER to stop
        session = r.stop()

        if not session.get("events"):
            print("  No events were recorded. Please try again.")
            return

        print("\nProcessing session...")
        phase_a_result = analyze_session(session["events"], session["audio_path"])
        answers = ask_in_terminal(phase_a_result)
        plan, warnings = complete_plan(phase_a_result, answers)
        for w in warnings:
            print(f"  Warning: {w}")

        print(f"\n{'-'*50}")
        print(f"  Source : {plan.get('source_platform')}")
        print(f"  Target : {plan.get('target_platform')}")
        print(f"  Steps  : {len(plan.get('steps', []))}")
        print(f"{'-'*50}")

        print("\nRun the process now? (y/n): ", end="")
        if input().strip().lower() != "y":
            print("  Plan saved to sessions/plan.json -- run again and choose option 2.")
            return

    elif choice == "2":
        plan_path = Path("sessions/plan.json")
        if not plan_path.exists():
            print("  No saved plan found. Record one first (option 1).")
            return
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
        print(f"\n  Plan loaded: {plan.get('goal')}")
        print(f"     Source -> Target: {plan.get('source_platform')} -> {plan.get('target_platform')}")
        print(f"     Steps: {len(plan.get('steps', []))}")

    elif choice == "3":
        try:
            from database.db import get_history, get_statistics
            stats = get_statistics()
            print(f"\n  Overall statistics:")
            print(f"     Total sessions : {stats['total_sessions']}")
            print(f"     Success rate   : {stats['success_rate_pct']}%")
            print(f"     Active plans   : {stats['active_plans']}")
            print(f"\n  Last 10 sessions:")
            for s in get_history(10):
                print(f"     {s['created_at'][:16]}  {s.get('source_platform','?')} -> {s.get('target_platform','?')}  {s['successful_steps']}/{s['step_count']} ok")
        except Exception as e:
            print(f"  Error reading SQLite: {e}")
        return

    else:
        print("  Invalid option.")
        return

    # complete_plan() stores no plain-text credentials in the plan -- only
    # secret_id references in credential_refs. Resolve them here, right
    # before execution, so the plain-text value exists in memory for as
    # little time as possible.
    from database.db import read_secret
    credentials = {}
    for field, secret_id in plan.get("credential_refs", {}).items():
        value = read_secret(secret_id)
        if value:
            credentials[field] = value
    for item in plan.get("required_credentials", []):
        if credentials.get(item):
            continue
        is_password = any(w in item.lower() for w in ["password", "pass", "secret"])
        value = getpass.getpass(f"  {item}: ") if is_password else input(f"  {item}: ").strip()
        credentials[item] = value

    email = input("\n  Email for the report (press Enter to skip): ").strip()

    results = await run_agent(plan, credentials, email)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n  Done: {ok}/{len(results)} steps successful")


if __name__ == "__main__":
    asyncio.run(main())
