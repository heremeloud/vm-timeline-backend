# -------------------------------------------------------
# EVENT CATEGORIES
# To add or remove a category, just edit this list.
# Changes here are automatically reflected in the API
# validation and (if kept in sync) the frontend dropdown.
# -------------------------------------------------------

EVENT_CATEGORIES: list[str] = [
    "show",
    "live",
    "press tour",
    "event",
    "fan event",
]

EVENT_SUBCATEGORIES: dict[str, list[str]] = {
    "show": ["interview", "variety", "talk"],
    "event": ["brand event", "promotional event", "award show", "gmmtv"],
    "fan event": ["fan sign", "fan meet", "fan fest"],
}

PROJECT_CATEGORIES: list[str] = [
    "series",
    "concert",
    "movie",
    "variety",
    "song",
    "music video",
    "other",
]
