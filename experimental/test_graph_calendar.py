"""
Test script: Check if you can create calendar events via Microsoft Graph API
using interactive browser login (delegated permissions, no admin consent needed).

Usage:
    python test_graph_calendar.py

What it does:
    1. Opens a browser for you to sign in with your Microsoft account
    2. Attempts to create a test calendar event (30 min, just for you, no attendees)
    3. If that works, creates a second test event WITH an attendee (sends a real invite)
    4. Reports success/failure at each step

If step 2 succeeds but step 3 fails with a permissions error, your tenant
allows calendar access but blocks sending invites programmatically.
"""

import json
import sys
from datetime import datetime, timedelta

from azure.identity import InteractiveBrowserCredential
import requests

# Microsoft Graph endpoints
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Calendars.ReadWrite"]


def get_token() -> str:
    """Authenticate interactively and return an access token."""
    print("\n  [Auth] Opening browser for sign-in...")
    print("  [Auth] You only need to do this once per session.\n")

    credential = InteractiveBrowserCredential(
        # Use the well-known "Microsoft Graph Command Line Tools" client ID
        # This is a public client that doesn't require app registration
        client_id="14d82eec-204b-4c2f-b7e8-296a70dab67e",
        tenant_id="common",
    )

    token = credential.get_token("https://graph.microsoft.com/.default")
    print("  [Auth] Authenticated successfully.\n")
    return token.token


def create_test_event(token: str, with_attendee: str | None = None) -> dict:
    """Create a calendar event via Graph API."""
    now = datetime.utcnow()
    # Schedule the test event for tomorrow to avoid conflicts
    start = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)

    event_body = {
        "subject": "TEST — Graph API Calendar Access Check (safe to delete)",
        "body": {
            "contentType": "Text",
            "content": "This is a test event created by the Meeting Agent setup script. Safe to delete.",
        },
        "start": {
            "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "UTC",
        },
        "isOnlineMeeting": False,
    }

    if with_attendee:
        event_body["attendees"] = [
            {
                "emailAddress": {
                    "address": with_attendee,
                    "name": with_attendee,
                },
                "type": "required",
            }
        ]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{GRAPH_BASE}/me/events",
        headers=headers,
        json=event_body,
    )

    return {
        "status_code": response.status_code,
        "body": response.json() if response.content else {},
    }


def delete_event(token: str, event_id: str):
    """Clean up a test event."""
    headers = {"Authorization": f"Bearer {token}"}
    requests.delete(f"{GRAPH_BASE}/me/events/{event_id}", headers=headers)


def main():
    print("=" * 60)
    print("  GRAPH API CALENDAR TEST")
    print("  Checking if your tenant allows calendar event creation")
    print("=" * 60)

    # Step 1: Authenticate
    try:
        token = get_token()
    except Exception as e:
        print(f"  [FAIL] Authentication failed: {e}")
        print("  Your tenant may block interactive auth for public clients.")
        sys.exit(1)

    # Step 2: Create a basic event (no attendees)
    print("  [Test 1] Creating a test calendar event (no attendees)...")
    result = create_test_event(token)

    if result["status_code"] == 201:
        event_id = result["body"].get("id")
        print("  [PASS] Calendar event created successfully!")
        print(f"         Event ID: {event_id[:40]}...")

        # Clean up
        print("  [Cleanup] Deleting test event...")
        delete_event(token, event_id)
        print("  [Cleanup] Done.\n")
    else:
        print(f"  [FAIL] Status {result['status_code']}")
        error = result["body"].get("error", {})
        print(f"         Code: {error.get('code', 'unknown')}")
        print(f"         Message: {error.get('message', 'unknown')}")
        print("\n  Your tenant does not allow calendar access via Graph API.")
        print("  Stick with the .ics file approach.")
        sys.exit(1)

    # Step 3: Create an event WITH an attendee (this actually sends an invite)
    test_email = input("  [Test 2] Enter an email to send a TEST invite to\n"
                       "           (use your own email, or press Enter to skip): ").strip()

    if not test_email:
        print("\n  [Skip] Skipping attendee test.")
        print("\n  RESULT: Calendar creation works. Attendee invites not tested.")
        print("  You can update the agent to use Graph API for creating events.")
        return

    print(f"\n  [Test 2] Creating event with attendee: {test_email}...")
    result = create_test_event(token, with_attendee=test_email)

    if result["status_code"] == 201:
        event_id = result["body"].get("id")
        print("  [PASS] Calendar event with attendee created!")
        print(f"         An invite was sent to {test_email}")
        print(f"         Check their inbox to confirm.\n")

        # Don't auto-delete this one so the user can verify the invite arrived
        print(f"  NOTE: Test event left on your calendar. Delete it manually after verifying.\n")

        print("  " + "=" * 58)
        print("  RESULT: Full Graph API calendar support confirmed!")
        print("  The agent can be updated to send invites directly via Graph.")
        print("  " + "=" * 58)
    else:
        print(f"  [FAIL] Status {result['status_code']}")
        error = result["body"].get("error", {})
        print(f"         Code: {error.get('code', 'unknown')}")
        print(f"         Message: {error.get('message', 'unknown')}")
        print("\n  RESULT: Calendar creation works, but sending invites to others is blocked.")
        print("  Stick with the .ics file approach for now.")


if __name__ == "__main__":
    main()
