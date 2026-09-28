from google_calendar import get_calendar_service


service = get_calendar_service()

events = service.events().list(
    calendarId="primary",
    maxResults=5
).execute()

print("[SUCCESS] Google Calendar connected successfully!")

for event in events.get("items", []):
    print(
        event.get(
            "summary",
            "No title"
        )
    )