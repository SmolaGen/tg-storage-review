import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FileChunk(Base):
    __tablename__ = "file_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("storage_workers.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    tg_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("file_id", "position"),)
