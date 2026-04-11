from typing import Optional
from pydantic import BaseModel, field_validator

class SubscriptionCreate(BaseModel):
    name: str
    source_url: str          # newline-separated URLs, stored as-is in DB
    sync_interval_hours: int = 24
    list_type: str = "domain"
    tags: Optional[str] = ""
    notes: Optional[str] = ""
    allocated_slots: Optional[int] = None   # number of Firewalla lists; None = auto-calculate

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v):
        if not v.strip(): raise ValueError("Name cannot be empty")
        return v.strip()

    @field_validator("sync_interval_hours")
    @classmethod
    def valid_interval(cls, v):
        if v not in [1, 6, 12, 24, 48, 168]: raise ValueError("Invalid interval")
        return v

    @field_validator("list_type")
    @classmethod
    def valid_type(cls, v):
        if v not in ("domain", "ip", "mixed"): raise ValueError("Invalid list type")
        return v

class SubscriptionUpdate(BaseModel):
    name: Optional[str] = None
    source_url: Optional[str] = None   # newline-separated URLs
    sync_interval_hours: Optional[int] = None
    enabled: Optional[bool] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    allocated_slots: Optional[int] = None

    @field_validator("sync_interval_hours")
    @classmethod
    def valid_interval(cls, v):
        if v is not None and v not in [1, 6, 12, 24, 48, 168]: raise ValueError("Invalid interval")
        return v
