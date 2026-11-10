"""Sample-data tables for `MockPlacesProvider`. Nothing here describes a real venue.

Only the city centres are real, so that keyless development still gets
sensible weather and drive times for a familiar place name.
"""

from __future__ import annotations

from typing import NamedTuple


class VenueKind(NamedTuple):
    code: str  # embedded in the mock place_id
    noun: str
    types: tuple[str, ...]  # Google place types a venue of this kind would carry
    keywords: tuple[str, ...]  # query words that select this kind
    blurb: str


# (label, lowercase names to match in an address or query, lat, lng)
CITIES: tuple[tuple[str, tuple[str, ...], float, float], ...] = (
    ("Austin, TX", ("austin",), 30.2672, -97.7431),
    ("New York, NY", ("new york", "nyc", "manhattan", "brooklyn"), 40.7128, -74.0060),
    ("San Francisco, CA", ("san francisco", "sf bay"), 37.7749, -122.4194),
    ("Los Angeles, CA", ("los angeles",), 34.0522, -118.2437),
    ("Chicago, IL", ("chicago",), 41.8781, -87.6298),
    ("Seattle, WA", ("seattle",), 47.6062, -122.3321),
    ("Boston, MA", ("boston",), 42.3601, -71.0589),
    ("Denver, CO", ("denver",), 39.7392, -104.9903),
    ("Miami, FL", ("miami",), 25.7617, -80.1918),
    ("Atlanta, GA", ("atlanta",), 33.7490, -84.3880),
    ("Dallas, TX", ("dallas",), 32.7767, -96.7970),
    ("Houston, TX", ("houston",), 29.7604, -95.3698),
    ("Washington, DC", ("washington, dc", "washington dc"), 38.9072, -77.0369),
    ("Nashville, TN", ("nashville",), 36.1627, -86.7816),
    ("Las Vegas, NV", ("las vegas", "vegas"), 36.1699, -115.1398),
    ("Portland, OR", ("portland",), 45.5152, -122.6784),
    ("Toronto, ON", ("toronto",), 43.6532, -79.3832),
    ("Vancouver, BC", ("vancouver",), 49.2827, -123.1207),
    ("London, UK", ("london",), 51.5074, -0.1278),
    ("Paris, France", ("paris",), 48.8566, 2.3522),
    ("Berlin, Germany", ("berlin",), 52.5200, 13.4050),
    ("Amsterdam, Netherlands", ("amsterdam",), 52.3676, 4.9041),
    ("Madrid, Spain", ("madrid",), 40.4168, -3.7038),
    ("Dublin, Ireland", ("dublin",), 53.3498, -6.2603),
    ("Seoul, South Korea", ("seoul",), 37.5665, 126.9780),
    ("Tokyo, Japan", ("tokyo",), 35.6762, 139.6503),
    ("Singapore", ("singapore",), 1.3521, 103.8198),
    ("Sydney, Australia", ("sydney",), -33.8688, 151.2093),
    ("Mexico City, Mexico", ("mexico city",), 19.4326, -99.1332),
    ("Sao Paulo, Brazil", ("sao paulo", "são paulo"), -23.5505, -46.6333),
)

UNKNOWN_CITY = "Sampleville"

VENUE_KINDS: tuple[VenueKind, ...] = (
    VenueKind(
        "conf",
        "Conference Center",
        ("convention_center", "event_venue"),
        ("conference", "convention", "summit", "meeting", "seminar", "offsite", "corporate"),
        "a conference centre with a main hall and breakout rooms",
    ),
    VenueKind(
        "hall",
        "Event Hall",
        ("event_venue",),
        ("event", "party", "launch", "reception", "mixer"),
        "a flexible open-plan event hall",
    ),
    VenueKind(
        "banq",
        "Banquet Hall",
        ("banquet_hall", "event_venue"),
        ("banquet", "gala", "dinner", "awards"),
        "a banquet hall with in-house catering",
    ),
    VenueKind(
        "wedd",
        "Garden Pavilion",
        ("wedding_venue", "event_venue"),
        ("wedding", "garden", "outdoor", "ceremony"),
        "a garden pavilion with indoor and outdoor space",
    ),
    VenueKind(
        "hotl",
        "Hotel Ballroom",
        ("hotel", "lodging", "event_venue"),
        ("hotel", "ballroom", "retreat"),
        "a hotel ballroom with guest rooms on site",
    ),
    VenueKind(
        "loft",
        "Rooftop Loft",
        ("event_venue", "bar"),
        ("rooftop", "loft", "cocktail", "happy hour", "networking"),
        "a rooftop loft with skyline views",
    ),
    VenueKind(
        "audi",
        "Auditorium",
        ("auditorium", "event_venue"),
        ("auditorium", "theater", "theatre", "lecture", "talk", "keynote", "screening"),
        "a tiered-seating auditorium with a stage and AV booth",
    ),
)

NAME_STEMS = (
    "Juniper", "Harborlight", "Copperfield", "Larkspur", "Meridian", "Ironwood", "Bluebonnet", "Lanternhouse",
    "Northgate", "Silverline", "Wren & Oak", "Foundry", "Marigold", "Cascade", "Stonebridge", "Atlas",
)  # fmt: skip

# Street names nobody could mistake for a real address.
STREETS = ("Example Street", "Sample Avenue", "Placeholder Boulevard", "Demo Lane", "Mockingbird Way", "Fixture Road")

PARKING_FEATURES = (
    "Free parking lot", "Paid parking lot", "Free street parking", "Paid street parking",
    "Valet parking", "Paid garage parking",
)  # fmt: skip

ACCESSIBILITY_FEATURES = (
    "Wheelchair-accessible entrance", "Wheelchair-accessible parking",
    "Wheelchair-accessible restroom", "Wheelchair-accessible seating",
)  # fmt: skip

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

REVIEW_SNIPPETS = (
    "Sample review: the staff handled our AV setup quickly and the room layout was flexible.",
    "Sample review: great space, though parking filled up about half an hour before doors.",
    "Sample review: catering was on time and the load-in dock made setup easy.",
    "Sample review: acoustics in the main room were good; the breakout rooms run warm.",
    "Sample review: easy to reach by transit, and check-in for 150 guests took ten minutes.",
)

# (name, kind) - kinds follow `ParkingOption.kind`.
PARKING_SPOTS = (
    ("Parking Garage", "garage"),
    ("Surface Lot", "lot"),
    ("Street Parking Zone", "street"),
    ("Valet Stand", "valet"),
    ("Park & Walk Lot", "lot"),
    ("Public Garage", "garage"),
)
