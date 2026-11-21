from pydantic import BaseModel


class Webcam(BaseModel):
    id: str  # "<source>:<native id>"
    source: str
    name: str
    lat: float
    lon: float
    country: str = ""
    image_url: str
    video_url: str | None = None
    embed_url: str | None = None
    refresh_s: int = 60
    licence: str = ""
    attribution: str = ""
    commercial_ok: bool = False
    last_checked: str = ""

    def slim(self) -> dict:
        return {"id": self.id, "source": self.source, "name": self.name, "lat": self.lat, "lon": self.lon,
                "has_video": bool(self.video_url or self.embed_url)}
