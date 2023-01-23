"""Definition of data models used by Tightlock application."""

from datetime import datetime
from typing import Dict, Optional

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy_json import mutable_json_type
from sqlmodel import Column
from sqlmodel import DateTime
from sqlmodel import Field
from sqlmodel import SQLModel

# Represents a configured activation. This model is part of the config and does not define a table for now.
class Activation(SQLModel):
  name: str  # Activation name
  source_name: str  # Source name
  destination_name: str  # Destination name
  schedule: Optional[str] = None  # A cron expression or a cron preset (None for triggered)"


class Config(SQLModel, table=True):

  __table_args__ = (UniqueConstraint("label"),)

  id: Optional[int] = Field(default=None, primary_key=True)
  create_date: datetime = Field(sa_column=Column(DateTime(timezone=True)), default_factory=datetime.now, nullable=False)
  label: str
  # defined as below to avoid this: https://amercader.net/blog/beware-of-json-fields-in-sqlalchemy/
  value: Dict = Field(default={}, sa_column=Column(mutable_json_type(dbtype=JSONB, nested=True)))

  # Needed for Column(JSON)
  class Config:  # Inner `Config`` class is needed by sqlmodel. Same name as the parant class is a coincidence.
    arbitrary_types_allowed = True
