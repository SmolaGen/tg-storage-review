import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class StorageWorker(Base):
    __tablename__ = "storage_workers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    storage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("storages.id", ondelete="CASCADE"),
        nullable=False,
    )
    bot_token: Mapped[str] = mapped_column(Text, nullable=False)
    session_string: Mapped[str | None] = mapped_column(
        Text
    )  # INFR-02: StringSession persistence, nullable
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("storage_id", "bot_token"),)
