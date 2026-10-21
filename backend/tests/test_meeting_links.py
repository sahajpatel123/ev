"""Phase F foundation: meeting-join URLs from normalized calendar events."""

from app.digital.adapters.calendar import extract_meeting_links, normalize_event


def test_normalize_keeps_join_fields() -> None:
    event = normalize_event({
        "id": "evt-1",
        "summary": "Ship review",
        "location": "HQ room 3",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij", "label": "meet"},
                {"entryPointType": "phone", "uri": "tel:+1-555-0100", "label": "phone"},
            ]
        },
        "start": {"dateTime": "2026-10-12T10:00:00+00:00"},
        "end": {"dateTime": "2026-10-12T11:00:00+00:00"},
    })
    assert event["location"] == "HQ room 3"
    assert event["hangout_link"] == "https://meet.google.com/abc-defg-hij"
    assert event["conference_links"][0]["url"] == "https://meet.google.com/abc-defg-hij"


def test_normalize_missing_join_fields_default_empty() -> None:
    event = normalize_event({"id": "evt-2", "summary": "Focus"})
    assert event["location"] == ""
    assert event["hangout_link"] == ""
    assert event["conference_links"] == []
    assert extract_meeting_links(event) == []


def test_extract_classifies_providers_and_dedupes() -> None:
    event = normalize_event({
        "id": "evt-3",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "location": "https://teams.microsoft.com/l/meetup-join/xyz",
        "description": "Backup: https://zoom.us/j/123456789 and https://meet.google.com/abc-defg-hij again.",
    })
    links = extract_meeting_links(event)
    by_provider = {entry["provider"]: entry["url"] for entry in links}
    assert by_provider["meet"] == "https://meet.google.com/abc-defg-hij"
    assert by_provider["teams"] == "https://teams.microsoft.com/l/meetup-join/xyz"
    assert by_provider["zoom"] == "https://zoom.us/j/123456789"
    assert len(links) == 3  # hangout duplicate collapsed


def test_extract_unknown_hosts_are_generic_not_dropped() -> None:
    event = normalize_event({"description": "Join: https://meet.example-corp.internal/room-7)"})
    links = extract_meeting_links(event)
    assert links == [{"provider": "generic", "url": "https://meet.example-corp.internal/room-7"}]


def test_extract_ignores_non_urls_and_bad_shapes() -> None:
    assert extract_meeting_links({}) == []
    assert extract_meeting_links(None) == []
    event = normalize_event({"location": "HQ room 3", "description": "no links here"})
    assert extract_meeting_links(event) == []
