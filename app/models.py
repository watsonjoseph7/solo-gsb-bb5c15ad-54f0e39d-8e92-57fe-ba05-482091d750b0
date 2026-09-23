import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    commitments: Mapped[list["Commitment"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (
        UniqueConstraint("event_id", "commitment_hash", name="uq_commitments_event_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    commitment_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    event: Mapped[Event] = relationship(back_populates="commitments")
    reveal: Mapped[Optional["Reveal"]] = relationship(back_populates="commitment", uselist=False)


class DeadlineExtension(Base):
    """一次成功截止时间延长的审计记录（旧值/新值/原因），同时用于幂等重放判定。"""

    __tablename__ = "deadline_extensions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    old_commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    new_commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    new_reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())


class Reveal(Base):
    __tablename__ = "reveals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    # 每个承诺至多被揭示一次：唯一约束保证并发下「仅首次匹配的揭示生效」
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("commitments.id", ondelete="CASCADE"), unique=True
    )
    original_text: Mapped[str] = mapped_column(Text)
    salt: Mapped[str] = mapped_column(Text)
    recomputed_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    commitment: Mapped[Commitment] = relationship(back_populates="reveal")


class Settlement(Base):
    """事件 closed 后的永久结算记录（每事件至多一条）。

    标准结果一经确认即不可改写：event_id 唯一约束兜底并发下「仅首次结算生效」。
    correct / incorrect / unrevealed 三个计数在结算时刻快照写入，反映确认时
    全部已揭示承诺的统计；此后事件数据只读，计数保持不变。
    """

    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), unique=True
    )
    standard_result: Mapped[str] = mapped_column(Text)
    correct_count: Mapped[int] = mapped_column(Integer)
    incorrect_count: Mapped[int] = mapped_column(Integer)
    unrevealed_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())
