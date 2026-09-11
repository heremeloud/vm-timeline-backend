"""Event photo lists with per-image display focus and legacy URL support."""
import json
from datetime import date as CalendarDate
from typing import Optional
from pydantic import BaseModel, Field


class EventPhoto(BaseModel):
    url: str
    date: Optional[CalendarDate] = None
    focal_x: Optional[float] = Field(default=50, ge=0, le=100, allow_inf_nan=False)
    focal_y: Optional[float] = Field(default=50, ge=0, le=100, allow_inf_nan=False)


def clean_photos(items):
    photos = []
    seen = set()
    for item in items or []:
        photo = item if isinstance(item, EventPhoto) else EventPhoto(**item)
        url = photo.url.strip()
        key = (url, photo.date)
        if not url or key in seen:
            continue
        seen.add(key)
        photos.append({"url": url, "focal_x": photo.focal_x if photo.focal_x is not None else 50,
                       "focal_y": photo.focal_y if photo.focal_y is not None else 50})
        if photo.date is not None:
            photos[-1]["date"] = photo.date.isoformat()
    return photos


def event_photos(event):
    photos = clean_photos(json.loads(event.photo_items_json or "[]"))
    if photos:
        return photos
    urls = json.loads(event.media_urls_json or "[]") or ([event.media_url] if event.media_url else [])
    return clean_photos([{"url": url, "focal_x": event.media_focal_x if i == 0 else 50,
                         "focal_y": event.media_focal_y if i == 0 else 50}
                        for i, url in enumerate(urls)])


def set_event_photos(event, photos):
    event.photo_items_json = json.dumps(photos)
    event.media_urls_json = json.dumps([photo["url"] for photo in photos])
    event.media_url = photos[0]["url"] if photos else None
    event.media_focal_x = photos[0]["focal_x"] if photos else None
    event.media_focal_y = photos[0]["focal_y"] if photos else None
