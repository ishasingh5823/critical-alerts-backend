"""Parse the *end-client* from a Joveo campaign name.

Joveo's campaign-naming convention is `Joveo | <reseller?> | <end_client> | <platform> | ...`
but several variations exist. End-clients matter because multiple distinct
brands often live inside the same Meta/Google ad account (e.g., Monster
holds Trimac/Amrize/FLINT/David Lloyd/NatWest; CAE Google account holds
multiple campaigns for different products).

The parser:
  1. Splits the campaign name on " | "
  2. Skips a leading "Joveo" if present
  3. Skips any segment in the KNOWN_RESELLERS set
  4. Returns the next segment as end_client
  5. Returns None if no reasonable end-client can be identified

Resellers are agencies / programs that own the ad account or campaign
namespace but aren't themselves the end-customer. Maintain this set as
new resellers are spotted — the rule is "this name appears in many
campaigns for different brands."
"""

from __future__ import annotations

from typing import Optional

# Treated as middlemen — not end-clients. Add new ones here as we spot them.
# Discovered 2026-05-27 from audit of `Joveo | <agency> | <client> | ...` campaigns:
KNOWN_RESELLERS = {
    "joveo",
    # major agencies
    "ab&c",
    "ab&c creative",
    "ams",
    "yoke",
    "we are yoke",
    "hirevalue",
    "hire value",
    "banfield",
    # smaller agencies / owner handles (lowercase = agency, Title Case = client)
    "wiser",
    "swish",
    "cielo",
    "blu ivy",
    "clickup",
    "twochairs",
    "motional",
}

# If the segment matches one of these (case-insensitive), it's a platform
# marker not a client. We stop walking the segments when we hit one.
PLATFORM_MARKERS = {
    "meta",
    "google",
    "bing",
    "linkedin",
    "tiktok",
    "facebook",
    "microsoft",
    "instagram",
    "fb",
    "ig",
}


def parse_end_client(campaign_name: Optional[str]) -> Optional[str]:
    """Return the end-client brand from a Joveo-format campaign name, or
    None if it can't be identified. Handles two real-world naming styles:

    1. **Joveo-prefixed (standard)**: `Joveo | <reseller?> | <end_client> | <platform> | ...`
       e.g. "Joveo | AB&C | Kenvue | Meta | Colombia"  → "Kenvue"
       e.g. "Joveo | Scale AI | Meta | Japan"          → "Scale AI"
       e.g. "Joveo | Yoke | David Lloyd Clubs | Meta"  → "David Lloyd Clubs"

    2. **Non-prefixed pipe format (legacy / naming-mistake)**: `<end_client> | <something> | ...`
       e.g. "Ashley Furniture | Hiring Event | May 19-21"  → "Ashley Furniture"
       Joveo's social team sometimes ships campaigns without the Joveo
       prefix — we still treat these as real client work as long as the
       first segment isn't a platform marker.

    Rejected (returns None — these aren't structured client campaigns):
      - No pipes at all: "G_Horsham_RN_NB_CONV", "Career Site Builder - Aug 2025"
      - Only platform markers: "Joveo | Meta | ..."
      - Empty / null inputs

    Maintain KNOWN_RESELLERS as new agency / pipeline names are spotted.
    """
    if not campaign_name:
        return None
    segments = [s.strip() for s in campaign_name.split("|") if s.strip()]
    # Require pipe-structured names — single-segment campaigns are usually
    # ad-platform internal IDs or Joveo's own marketing, not client work.
    if len(segments) < 2:
        return None

    # Case 1: starts with "Joveo" — skip Joveo + resellers, take next
    if segments[0].lower() == "joveo":
        i = 1
        while i < len(segments) and segments[i].lower() in KNOWN_RESELLERS:
            i += 1
        if i >= len(segments):
            return None
        candidate = segments[i]
    else:
        # Case 2: no Joveo prefix — first segment is the client (if it's
        # not a reseller or platform marker). Naming-mistake fallback.
        if segments[0].lower() in KNOWN_RESELLERS:
            # Skip reseller, take next (rare)
            if len(segments) < 2:
                return None
            candidate = segments[1]
        else:
            candidate = segments[0]

    if candidate.lower() in PLATFORM_MARKERS:
        return None
    return candidate


def resolve_end_client(campaign_name: Optional[str],
                       fallback_display_name: Optional[str] = None
                       ) -> Optional[str]:
    """Same as parse_end_client but falls back to the ad-account's display
    name if parsing fails. Used when populating end_client at sync time."""
    parsed = parse_end_client(campaign_name)
    if parsed:
        return parsed
    return fallback_display_name
