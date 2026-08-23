"""Curated AppLaunchRegistry. Public launch surfaces only. No private schemes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppLaunchEntry:
    app_id: str
    display_name: str
    aliases: tuple[str, ...]
    universal_links: tuple[str, ...]
    url_schemes: tuple[str, ...]
    fallback_web_url: str | None
    source: str
    category: str


# Documented or universal-link surfaces only. Unverified private schemes are omitted.
APP_LAUNCH_REGISTRY: tuple[AppLaunchEntry, ...] = (
    AppLaunchEntry(
        "phone",
        "Phone",
        ("phone", "phone app"),
        (),
        ("tel://",),
        None,
        "system",
        "apple",
    ),
    AppLaunchEntry(
        "messages",
        "Messages",
        ("messages", "imessage", "sms", "texts"),
        (),
        ("sms:",),
        None,
        "system",
        "apple",
    ),
    AppLaunchEntry(
        "facetime",
        "FaceTime",
        ("facetime",),
        (),
        ("facetime://",),
        None,
        "system",
        "apple",
    ),
    AppLaunchEntry(
        "safari",
        "Safari",
        ("safari", "browser"),
        ("https://www.apple.com",),
        (),
        "https://www.apple.com",
        "https",
        "apple",
    ),
    AppLaunchEntry(
        "maps",
        "Maps",
        ("maps", "apple maps"),
        ("https://maps.apple.com/",),
        (),
        "https://maps.apple.com",
        "apple-maps",
        "apple",
    ),
    AppLaunchEntry(
        "music",
        "Music",
        ("music", "apple music"),
        ("https://music.apple.com",),
        (),
        "https://music.apple.com",
        "universal-link",
        "apple",
    ),
    AppLaunchEntry(
        "photos",
        "Photos",
        ("photos", "photo library"),
        (),
        (),
        None,
        "unsupported-without-native",
        "apple",
    ),
    AppLaunchEntry(
        "calendar",
        "Calendar",
        ("calendar", "cal"),
        (),
        (),
        None,
        "eventkit-preferred",
        "apple",
    ),
    AppLaunchEntry(
        "notes",
        "Notes",
        ("notes", "apple notes"),
        (),
        (),
        None,
        "unsupported-without-native",
        "apple",
    ),
    AppLaunchEntry(
        "youtube",
        "YouTube",
        ("youtube", "yt"),
        ("https://www.youtube.com",),
        (),
        "https://www.youtube.com",
        "universal-link",
        "media",
    ),
    AppLaunchEntry(
        "spotify",
        "Spotify",
        ("spotify",),
        ("https://open.spotify.com",),
        (),
        "https://open.spotify.com",
        "universal-link",
        "media",
    ),
    AppLaunchEntry(
        "instagram",
        "Instagram",
        ("instagram", "insta"),
        ("https://www.instagram.com",),
        (),
        "https://www.instagram.com",
        "universal-link",
        "social",
    ),
    AppLaunchEntry(
        "whatsapp",
        "WhatsApp",
        ("whatsapp", "whats app"),
        ("https://wa.me",),
        (),
        "https://wa.me",
        "universal-link",
        "communication",
    ),
    AppLaunchEntry(
        "gmail",
        "Gmail",
        ("gmail", "google mail"),
        ("https://mail.google.com",),
        (),
        "https://mail.google.com",
        "universal-link",
        "productivity",
    ),
    AppLaunchEntry(
        "chrome",
        "Chrome",
        ("chrome", "google chrome"),
        ("https://www.google.com",),
        (),
        "https://www.google.com",
        "universal-link",
        "productivity",
    ),
    AppLaunchEntry(
        "x",
        "X",
        ("x", "twitter"),
        ("https://x.com", "https://twitter.com"),
        (),
        "https://x.com",
        "universal-link",
        "social",
    ),
)


def resolve_app(query: str) -> AppLaunchEntry | None:
    needle = (query or "").strip().lower()
    if not needle:
        return None
    if needle in {"the app", "an app", "app"}:
        return None
    exact: list[AppLaunchEntry] = []
    for entry in APP_LAUNCH_REGISTRY:
        names = {entry.app_id, entry.display_name.lower(), *entry.aliases}
        if needle in names:
            exact.append(entry)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    # Alias containment only when unique and token-length is enough to avoid "in" → Instagram.
    if len(needle) < 3:
        return None
    hits = [
        entry
        for entry in APP_LAUNCH_REGISTRY
        if needle == entry.display_name.lower() or needle in entry.aliases
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def launch_url_for(entry: AppLaunchEntry) -> str | None:
    if entry.universal_links:
        return entry.universal_links[0].rstrip()
    if entry.fallback_web_url:
        return entry.fallback_web_url
    if entry.url_schemes:
        scheme = entry.url_schemes[0]
        if scheme.endswith("://"):
            return scheme
        if scheme.endswith(":"):
            return scheme
        return scheme
    return None
