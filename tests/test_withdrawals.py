from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import create_or_get_user, get_pending_withdrawals, get_user_by_id
from bot.database import create_all_tables, create_session_factory
from bot.models import Base, WithdrawalStatus
from bot.withdrawals import (
    WithdrawalPersistenceError,
    WithdrawalStates,
    WithdrawalValidationError,
    build_admin_withdrawal_keyboard,
    build_admin_withdrawal_text,
    handle_withdraw_paid_callback,
    list_pending_withdrawals,
    mark_manual_payout_paid,
    parse_withdrawal_amount,
    parse_withdrawal_callback_data,
    process_admin_tx_hash,
    reject_withdrawal,
    request_withdrawal,
    validate_ton_tx_hash,
    validate_ton_wallet,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_all_tables(engine)
    try:
        yield create_session_factory(engine)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class FakeCallback:
    def __init__(self, data: str, user_id: int = 9001) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[dict] = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


class FakeState:
    def __init__(self) -> None:
        self.data = {}
        self.state = None
        self.cleared = False

    async def clear(self):
        self.data = {}
        self.state = None
        self.cleared = True

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def get_data(self):
        return dict(self.data)


class FakeMessage:
    def __init__(self, text: str, user_id: int = 9001) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str):
        self.answers.append(text)


class FakeBot:
    async def send_message(self, **_kwargs):
        return None


def test_withdrawal_validation_helpers() -> None:
    assert parse_withdrawal_amount("25") == 25
    assert validate_ton_wallet("EQD1234567890abcdefghi") == "EQD1234567890abcdefghi"
    assert validate_ton_tx_hash("tx_1234567890abcdef") == "tx_1234567890abcdef"
    assert parse_withdrawal_callback_data("withdraw_paid:123", "withdraw_paid") == 123

    with pytest.raises(WithdrawalValidationError):
        parse_withdrawal_amount("0")
    with pytest.raises(WithdrawalValidationError):
        validate_ton_wallet("short")
    with pytest.raises(WithdrawalValidationError):
        validate_ton_tx_hash("short")


@pytest.mark.asyncio
async def test_request_withdrawal_reserves_stars_and_creates_pending_request(
    session_factory,
) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        user = await get_user_by_id(session, 101)
        user.balance_credits = 100
        await session.flush()

        result = await request_withdrawal(
            session=session,
            user_id=101,
            stars_amount=40,
            ton_wallet_address="EQD1234567890abcdefghi",
        )

        user = await get_user_by_id(session, 101)
        pending = await list_pending_withdrawals(session)

    assert result.withdrawal.status == WithdrawalStatus.PENDING
    assert result.withdrawal.credits_amount == 40
    assert result.withdrawal.ton_wallet_address == "EQD1234567890abcdefghi"
    assert result.remaining_balance == 60
    assert user is not None
    assert user.balance_credits == 60
    assert [withdrawal.id for withdrawal in pending] == [result.withdrawal.id]


@pytest.mark.asyncio
async def test_request_withdrawal_rejects_insufficient_balance(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")

        with pytest.raises(WithdrawalValidationError):
            await request_withdrawal(
                session=session,
                user_id=101,
                stars_amount=1,
                ton_wallet_address="EQD1234567890abcdefghi",
            )


@pytest.mark.asyncio
async def test_mark_manual_payout_paid_records_ton_tx_without_refund(
    session_factory,
) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        user = await get_user_by_id(session, 101)
        user.balance_credits = 100
        await session.flush()
        request = await request_withdrawal(session, 101, 40, "EQD1234567890abcdefghi")

        result = await mark_manual_payout_paid(
            session=session,
            withdrawal_id=request.withdrawal.id,
            admin_id=9001,
            ton_tx_hash="tx_1234567890abcdef",
        )

        user = await get_user_by_id(session, 101)

    assert result.withdrawal.status == WithdrawalStatus.COMPLETED
    assert result.withdrawal.admin_id == 9001
    assert result.withdrawal.ton_tx_hash == "tx_1234567890abcdef"
    assert user is not None
    assert user.balance_credits == 60


@pytest.mark.asyncio
async def test_reject_withdrawal_returns_reserved_stars(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        user = await get_user_by_id(session, 101)
        user.balance_credits = 100
        await session.flush()
        request = await request_withdrawal(session, 101, 40, "EQD1234567890abcdefghi")

        result = await reject_withdrawal(
            session=session,
            withdrawal_id=request.withdrawal.id,
            admin_id=9001,
            admin_note="bad wallet",
        )

        user = await get_user_by_id(session, 101)

    assert result.withdrawal.status == WithdrawalStatus.FAILED
    assert result.withdrawal.admin_note == "bad wallet"
    assert user is not None
    assert user.balance_credits == 100


@pytest.mark.asyncio
async def test_handle_withdraw_paid_callback_sets_admin_tx_hash_state(
    session_factory,
) -> None:
    callback = FakeCallback("withdraw_paid:123", user_id=9001)
    state = FakeState()

    async with session_factory() as session:
        await handle_withdraw_paid_callback(callback, session, state, admin_ids=[9001])

    assert callback.answers == [{"text": "Send TON transaction hash.", "show_alert": False}]
    assert state.data == {"withdrawal_id": 123}
    assert state.state == WithdrawalStates.waiting_admin_tx_hash


@pytest.mark.asyncio
async def test_handle_withdraw_paid_callback_denies_non_admin(session_factory) -> None:
    callback = FakeCallback("withdraw_paid:123", user_id=202)
    state = FakeState()

    async with session_factory() as session:
        await handle_withdraw_paid_callback(callback, session, state, admin_ids=[9001])

    assert callback.answers == [{"text": "Access denied", "show_alert": True}]
    assert state.data == {}


@pytest.mark.asyncio
async def test_process_admin_tx_hash_wraps_completion_failures(
    session_factory,
    monkeypatch,
    caplog,
) -> None:
    async def fail_paid_transition(*_args, **_kwargs):
        raise WithdrawalPersistenceError("db is tired")

    monkeypatch.setattr("bot.withdrawals.mark_manual_payout_paid", fail_paid_transition)
    caplog.set_level("ERROR", logger="bot.withdrawals")
    message = FakeMessage("tx_1234567890abcdef", user_id=9001)
    state = FakeState()
    state.data = {"withdrawal_id": 123}

    async with session_factory() as session:
        with pytest.raises(WithdrawalPersistenceError):
            await process_admin_tx_hash(
                message=message,
                session=session,
                state=state,
                bot=FakeBot(),
                admin_ids=[9001],
            )

    assert message.answers == ["Could not mark this payout paid. Please try again."]
    assert state.cleared is True
    assert "Admin tx hash payout completion failed" in caplog.text


def test_admin_withdrawal_text_uses_stars_and_ton_language() -> None:
    withdrawal = SimpleNamespace(
        id=123,
        user_id=101,
        credits_amount=40,
        ton_wallet_address="EQD1234567890abcdefghi",
    )

    text = build_admin_withdrawal_text(withdrawal)
    keyboard = build_admin_withdrawal_keyboard(123)

    assert "Amount: 40 Stars" in text
    assert "TON wallet" in text
    assert "credits" not in text.lower()
    assert keyboard.inline_keyboard[0][0].callback_data == "withdraw_paid:123"
    assert keyboard.inline_keyboard[0][1].callback_data == "withdraw_reject:123"


@pytest.mark.asyncio
async def test_request_withdrawal_wraps_unexpected_errors(
    session_factory,
    monkeypatch,
    caplog,
) -> None:
    async def fail_user_lock(*_args, **_kwargs):
        raise RuntimeError("lock exploded")

    monkeypatch.setattr("bot.withdrawals._get_user_for_update", fail_user_lock)
    caplog.set_level("ERROR", logger="bot.withdrawals")

    async with session_factory() as session:
        with pytest.raises(WithdrawalPersistenceError):
            await request_withdrawal(
                session=session,
                user_id=101,
                stars_amount=1,
                ton_wallet_address="EQD1234567890abcdefghi",
            )

    assert "Withdrawal operation failed unexpectedly: request_withdrawal" in caplog.text
