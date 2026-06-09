from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DepositStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REFUNDED = "refunded"


class MarketStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class WithdrawalStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class PayoutStatus(StrEnum):
    HELD = "held"
    RELEASED = "released"


class LedgerEntryType(StrEnum):
    BALANCE_ADJUSTMENT = "balance_adjustment"
    PAYOUT_HOLD = "payout_hold"
    PLATFORM_FEE = "platform_fee"


class DisputeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"


def enum_column(enum_type: type[StrEnum], length: int = 32) -> SAEnum:
    return SAEnum(
        enum_type,
        values_callable=lambda enum_class: [member.value for member in enum_class],
        native_enum=False,
        length=length,
    )


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    balance_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    is_banned: Mapped[bool] = mapped_column(nullable=False, default=False)

    deposits: Mapped[list[Deposit]] = relationship(back_populates="user")
    markets_created: Mapped[list[Market]] = relationship(
        back_populates="creator",
        foreign_keys="Market.creator_id",
    )
    bets: Mapped[list[Bet]] = relationship(back_populates="user")
    payouts: Mapped[list[Payout]] = relationship(back_populates="user")
    withdrawals: Mapped[list[Withdrawal]] = relationship(back_populates="user")
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(back_populates="user")
    disputes_raised: Mapped[list[Dispute]] = relationship(
        back_populates="raiser",
        foreign_keys="Dispute.raised_by",
    )


class Deposit(Base):
    __tablename__ = "deposits"
    __table_args__ = (
        UniqueConstraint("charge_id", name="uq_deposits_charge_id"),
        Index("ix_deposits_user_status_created", "user_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    charge_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DepositStatus] = mapped_column(
        enum_column(DepositStatus),
        nullable=False,
        default=DepositStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user: Mapped[User] = relationship(back_populates="deposits")


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (
        Index("ix_markets_chat_status_deadline", "chat_id", "status", "deadline"),
        Index("ix_markets_status_deadline", "status", "deadline"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inline_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    min_bet: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[MarketStatus] = mapped_column(
        enum_column(MarketStatus),
        nullable=False,
        default=MarketStatus.ACTIVE,
    )
    winning_option: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator: Mapped[User] = relationship(
        back_populates="markets_created",
        foreign_keys=[creator_id],
    )
    bets: Mapped[list[Bet]] = relationship(back_populates="market")
    payouts: Mapped[list[Payout]] = relationship(back_populates="market")
    disputes: Mapped[list[Dispute]] = relationship(back_populates="market")


class Bet(Base):
    __tablename__ = "bets"
    __table_args__ = (
        UniqueConstraint("user_id", "market_id", name="uq_bets_user_market"),
        Index("ix_bets_market_option", "market_id", "option_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("markets.id", ondelete="CASCADE"),
        nullable=False,
    )
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user: Mapped[User] = relationship(back_populates="bets")
    market: Mapped[Market] = relationship(back_populates="bets")


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        Index("ix_payouts_market_user", "market_id", "user_id"),
        Index("ix_payouts_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("markets.id", ondelete="CASCADE"),
        nullable=False,
    )
    credits_won: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        enum_column(PayoutStatus),
        nullable=False,
        default=PayoutStatus.HELD,
    )
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user: Mapped[User] = relationship(back_populates="payouts")
    market: Mapped[Market] = relationship(back_populates="payouts")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),
        Index("ix_ledger_entries_user_created", "user_id", "created_at"),
        Index("ix_ledger_entries_source", "source_table", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=True,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="XTR")
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        enum_column(LedgerEntryType),
        nullable=False,
    )
    source_table: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    user: Mapped[User | None] = relationship(back_populates="ledger_entries")


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    __table_args__ = (Index("ix_withdrawals_user_status_created", "user_id", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    credits_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    charge_ids_used: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    ton_wallet_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ton_tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[WithdrawalStatus] = mapped_column(
        enum_column(WithdrawalStatus),
        nullable=False,
        default=WithdrawalStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="withdrawals")


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (Index("ix_disputes_market_status_created", "market_id", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("markets.id", ondelete="CASCADE"),
        nullable=False,
    )
    raised_by: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DisputeStatus] = mapped_column(
        enum_column(DisputeStatus),
        nullable=False,
        default=DisputeStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    market: Mapped[Market] = relationship(back_populates="disputes")
    raiser: Mapped[User] = relationship(
        back_populates="disputes_raised",
        foreign_keys=[raised_by],
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint("kind", "market_id", "user_id", name="uq_notification_logs_kind_market_user"),
        Index("ix_notification_logs_kind_sent", "kind", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


JsonDict = dict[str, Any]
