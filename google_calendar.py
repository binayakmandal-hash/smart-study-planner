import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# Permission to create and edit Google Calendar events
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events"
]


def get_calendar_service():

    creds = None

    # Check whether we already have a saved login
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file(
            "token.json",
            SCOPES
        )

    # If there is no valid login, ask Google to authorize us
    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:

            creds.refresh(Request())

        else:

            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json",
                SCOPES
            )

            creds = flow.run_local_server(port=0)

        # Save login information
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    # Connect to Google Calendar
    service = build(
        "calendar",
        "v3",
        credentials=creds
    )

    return service