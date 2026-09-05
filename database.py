from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)

from config import config


logger = logging.getLogger(
    "pocket_database"
)


engine = create_async_engine(
    config.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
)

Session = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


class Base(DeclarativeBase):
    pass


class User(Base):

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
    )

    username: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    first_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    pair: Mapped[str] = mapped_column(
        String(64),
        default="ANY",
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
        default=5,
    )

    auto_signals: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    access: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Signal(Base):

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    pair: Mapped[str] = mapped_column(
        String(64),
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
    )

    direction: Mapped[str] = mapped_column(
        String(8),
    )

    probability: Mapped[float] = mapped_column(
        Float,
    )

    quality: Mapped[float] = mapped_column(
        Float,
    )

    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
    )

    close_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="PENDING",
    )

    result: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
    )

    reasons: Mapped[str] = mapped_column(
        Text,
        default="[]",
    )


async def init_db():

    async with engine.begin() as connection:

        await connection.run_sync(
            Base.metadata.create_all
        )

        # Исправление старого INTEGER Telegram ID.
        if "postgresql" in config.database_url:

            await connection.exec_driver_sql(
                """
                DO $$
                BEGIN

                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'users'
                        AND column_name = 'telegram_id'
                        AND data_type <> 'bigint'
                    ) THEN

                        ALTER TABLE users
                        ALTER COLUMN telegram_id
                        TYPE BIGINT
                        USING telegram_id::BIGINT;

                    END IF;

                END
                $$;
                """
            )


async def ensure_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:

    async with Session() as session:

        user = (
            await session.execute(
                select(User).where(
                    User.telegram_id == telegram_id
                )
            )
        ).scalar_one_or_none()

        if user is None:

            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                pair="ANY",
                timeframe=5,
                auto_signals=True,
                access=True,
                blocked=False,
            )

            session.add(user)

        else:

            user.username = username
            user.first_name = first_name

        await session.commit()

        return user


async def get_user(
    telegram_id: int,
) -> User | None:

    async with Session() as session:

        return (
            await session.execute(
                select(User).where(
                    User.telegram_id == telegram_id
                )
            )
        ).scalar_one_or_none()


async def update_user(
    telegram_id: int,
    **values,
):

    async with Session() as session:

        user = (
            await session.execute(
                select(User).where(
                    User.telegram_id == telegram_id
                )
            )
        ).scalar_one_or_none()

        if not user:
            return

        for key, value in values.items():

            if hasattr(user, key):

                setattr(
                    user,
                    key,
                    value,
                )

        await session.commit()


async def get_access_users() -> list[User]:

    async with Session() as session:

        result = await session.execute(
            select(User).where(
                User.access.is_(True),
                User.blocked.is_(False),
            )
        )

        return list(
            result.scalars().all()
        )


async def save_signal(
    pair: str,
    timeframe: int,
    direction: str,
    probability: float,
    quality: float,
    entry_time: datetime,
    close_time: datetime,
    reasons: list[str],
):

    async with Session() as session:

        signal = Signal(
            pair=pair,
            timeframe=timeframe,
            direction=direction,
            probability=probability,
            quality=quality,
            entry_time=entry_time,
            close_time=close_time,
            status="PENDING",
            result=None,
            reasons=json.dumps(
                reasons,
                ensure_ascii=False,
            ),
        )

        session.add(signal)

        await session.commit()

        return signal.id


async def mark_expired_signals():

    """
    Переводит истёкшие PENDING сигналы в EXPIRED.

    WIN/LOSS здесь специально не придумываем:
    для настоящего результата необходимо сравнить
    цену закрытия с направлением сигнала.

    Пока EXPIRED используется как состояние,
    чтобы один и тот же сигнал не обрабатывался повторно.
    """

    now = datetime.now(timezone.utc)

    async with Session() as session:

        result = await session.execute(
            select(Signal).where(
                Signal.status == "PENDING",
                Signal.close_time <= now,
            )
        )

        signals = list(
            result.scalars().all()
        )

        for signal in signals:

            signal.status = "EXPIRED"

        await session.commit()

        return len(signals)


async def set_signal_result(
    signal_id: int,
    result: str,
):

    result = result.upper().strip()

    if result not in {
        "WIN",
        "LOSS",
    }:
        raise ValueError(
            "Результат должен быть WIN или LOSS."
        )

    async with Session() as session:

        signal = await session.get(
            Signal,
            signal_id,
        )

        if not signal:
            return False

        signal.result = result
        signal.status = result

        await session.commit()

        return True


async def get_signal_stats():

    async with Session() as session:

        total = (
            await session.scalar(
                select(
                    func.count(Signal.id)
                )
            )
            or 0
        )

        wins = (
            await session.scalar(
                select(
                    func.count(Signal.id)
                ).where(
                    Signal.result == "WIN"
                )
            )
            or 0
        )

        losses = (
            await session.scalar(
                select(
                    func.count(Signal.id)
                ).where(
                    Signal.result == "LOSS"
                )
            )
            or 0
        )

        decided = wins + losses

        winrate = (
            wins / decided * 100
            if decided
            else 0.0
        )

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "decided": decided,
        }


async def get_user_stats():

    async with Session() as session:

        total = (
            await session.scalar(
                select(
                    func.count(User.id)
                )
            )
            or 0
        )

        active = (
            await session.scalar(
                select(
                    func.count(User.id)
                ).where(
                    User.auto_signals.is_(True),
                    User.blocked.is_(False),
                )
            )
            or 0
        )

        blocked = (
            await session.scalar(
                select(
                    func.count(User.id)
                ).where(
                    User.blocked.is_(True)
                )
            )
            or 0
        )

        return {
            "total": total,
            "active": active,
            "blocked": blocked,
        }


async def get_recent_signals(
    limit: int = 10,
):

    limit = max(
        1,
        min(
            limit,
            100,
        ),
    )

    async with Session() as session:

        result = await session.execute(
            select(Signal)
            .order_by(
                Signal.id.desc()
            )
            .limit(limit)
        )

        return list(
            result.scalars().all()
        )


async def get_pair_stats():

    async with Session() as session:

        result = await session.execute(
            select(
                Signal.pair,
                func.count(Signal.id),
            )
            .group_by(
                Signal.pair
            )
            .order_by(
                func.count(
                    Signal.id
                ).desc()
            )
        )

        return result.all()


async def close_database():

    await engine.dispose()
