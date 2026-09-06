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


# ============================================================
# ENGINE
# ============================================================

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


# ============================================================
# BASE
# ============================================================

class Base(DeclarativeBase):
    pass


# ============================================================
# USER
# ============================================================

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
        nullable=False,
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
        nullable=False,
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
        default=5,
        nullable=False,
    )

    auto_signals: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    access: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
        nullable=False,
    )

    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
        nullable=False,
    )


# ============================================================
# SIGNAL
# ============================================================

class Signal(Base):

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    pair: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
    )

    direction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
    )

    # Только техническая уверенность.
    # НЕ WINRATE.
    probability: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    quality: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    close_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # PENDING / WIN / LOSS
    status: Mapped[str] = mapped_column(
        String(16),
        default="PENDING",
        nullable=False,
        index=True,
    )

    # WIN / LOSS
    result: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        index=True,
    )

    # Цена на момент сигнала.
    entry_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    # Цена закрытия экспирации.
    close_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    reasons: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )


# ============================================================
# INIT
# ============================================================

async def init_db():

    logger.info(
        "[DATABASE] Инициализация PostgreSQL..."
    )

    async with engine.begin() as connection:

        await connection.run_sync(
            Base.metadata.create_all
        )

        if "postgresql" in config.database_url:

            # telegram_id
            try:

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

            except Exception as exc:

                logger.warning(
                    "[DATABASE] telegram_id migration: %s",
                    exc,
                )

            # last_seen
            try:

                result = await connection.exec_driver_sql(
                    """
                    SELECT
                        column_name,
                        is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'users'
                    AND column_name = 'last_seen'
                    """
                )

                row = result.first()

                if row is None:

                    await connection.exec_driver_sql(
                        """
                        ALTER TABLE users
                        ADD COLUMN last_seen
                        TIMESTAMP WITH TIME ZONE;
                        """
                    )

                await connection.exec_driver_sql(
                    """
                    UPDATE users
                    SET last_seen = COALESCE(
                        last_seen,
                        created_at,
                        NOW()
                    )
                    WHERE last_seen IS NULL;
                    """
                )

                await connection.exec_driver_sql(
                    """
                    ALTER TABLE users
                    ALTER COLUMN last_seen
                    SET DEFAULT NOW();
                    """
                )

                await connection.exec_driver_sql(
                    """
                    ALTER TABLE users
                    ALTER COLUMN last_seen
                    SET NOT NULL;
                    """
                )

            except Exception as exc:

                logger.exception(
                    "[DATABASE] last_seen migration: %s",
                    exc,
                )

                raise

            # Signal prices
            try:

                await connection.exec_driver_sql(
                    """
                    ALTER TABLE signals
                    ADD COLUMN IF NOT EXISTS
                    entry_price DOUBLE PRECISION;
                    """
                )

                await connection.exec_driver_sql(
                    """
                    ALTER TABLE signals
                    ADD COLUMN IF NOT EXISTS
                    close_price DOUBLE PRECISION;
                    """
                )

            except Exception as exc:

                logger.exception(
                    "[DATABASE] signal price migration: %s",
                    exc,
                )

                raise

    logger.info(
        "[DATABASE] ✅ PostgreSQL готов."
    )


# ============================================================
# CLOSE
# ============================================================

async def close_database():

    await engine.dispose()

    logger.info(
        "[DATABASE] Соединения закрыты."
    )


# ============================================================
# USER
# ============================================================

async def ensure_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:

    now = datetime.now(
        timezone.utc
    )

    async with Session() as session:

        user = (
            await session.execute(
                select(User).where(
                    User.telegram_id
                    == telegram_id
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
                created_at=now,
                last_seen=now,
            )

            session.add(user)

        else:

            user.username = username
            user.first_name = first_name
            user.last_seen = now

        await session.commit()

        return user


async def get_user(
    telegram_id: int,
) -> User | None:

    async with Session() as session:

        return (
            await session.execute(
                select(User).where(
                    User.telegram_id
                    == telegram_id
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
                    User.telegram_id
                    == telegram_id
                )
            )
        ).scalar_one_or_none()

        if user is None:
            return

        for key, value in values.items():

            if hasattr(
                user,
                key,
            ):

                setattr(
                    user,
                    key,
                    value,
                )

        user.last_seen = datetime.now(
            timezone.utc
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


# ============================================================
# SIGNAL SAVE
# ============================================================

async def save_signal(
    pair: str,
    timeframe: int,
    direction: str,
    probability: float,
    quality: float,
    entry_time: datetime,
    close_time: datetime,
    reasons: list[str],
    entry_price: float | None = None,
):

    async with Session() as session:

        signal = Signal(
            pair=pair,
            timeframe=int(timeframe),
            direction=str(direction).upper(),
            probability=float(
                probability
            ),
            quality=float(
                quality
            ),
            entry_time=entry_time,
            close_time=close_time,
            status="PENDING",
            result=None,
            entry_price=(
                float(entry_price)
                if entry_price is not None
                else None
            ),
            close_price=None,
            reasons=json.dumps(
                reasons,
                ensure_ascii=False,
            ),
        )

        session.add(signal)

        await session.commit()

        return signal.id


# ============================================================
# PENDING
# ============================================================

async def get_pending_signals():

    async with Session() as session:

        result = await session.execute(
            select(Signal)
            .where(
                Signal.status == "PENDING"
            )
            .order_by(
                Signal.close_time.asc()
            )
        )

        return list(
            result.scalars().all()
        )


async def get_signal(
    signal_id: int,
) -> Signal | None:

    async with Session() as session:

        return await session.get(
            Signal,
            signal_id,
        )


# ============================================================
# RESULT
# ============================================================

async def set_signal_result(
    signal_id: int,
    result: str,
    close_price: float | None = None,
):

    result = str(
        result
    ).upper().strip()

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

        if signal is None:
            return False

        if signal.status in {
            "WIN",
            "LOSS",
        }:

            return False

        signal.result = result
        signal.status = result

        if close_price is not None:

            signal.close_price = float(
                close_price
            )

        await session.commit()

        return True


async def settle_signal_by_price(
    signal_id: int,
    close_price: float,
) -> str | None:

    async with Session() as session:

        signal = await session.get(
            Signal,
            signal_id,
        )

        if signal is None:
            return None

        if signal.status != "PENDING":
            return signal.result

        if signal.entry_price is None:
            return None

        entry_price = float(
            signal.entry_price
        )

        close_price = float(
            close_price
        )

        if close_price == entry_price:
            # Ничья не влияет на WINRATE.
            return None

        direction = (
            str(signal.direction)
            .upper()
        )

        if direction == "UP":

            result = (
                "WIN"
                if close_price > entry_price
                else "LOSS"
            )

        elif direction == "DOWN":

            result = (
                "WIN"
                if close_price < entry_price
                else "LOSS"
            )

        else:

            return None

        signal.result = result
        signal.status = result
        signal.close_price = close_price

        await session.commit()

        return result


# ============================================================
# GLOBAL WINRATE
# ============================================================

async def get_signal_stats():

    async with Session() as session:

        total = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                )
            )
            or 0
        )

        wins = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.result == "WIN"
                )
            )
            or 0
        )

        losses = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.result == "LOSS"
                )
            )
            or 0
        )

        decided = (
            int(wins)
            + int(losses)
        )

        winrate = (
            float(wins)
            / float(decided)
            * 100.0
            if decided
            else None
        )

        return {
            "total": int(total),
            "wins": int(wins),
            "losses": int(losses),
            "decided": int(decided),
            "winrate": winrate,
        }


# ============================================================
# PAIR WINRATE
# ============================================================

async def get_pair_stats():

    async with Session() as session:

        result = await session.execute(
            select(
                Signal.pair,
                func.count(
                    Signal.id
                ).label("total"),
                func.sum(
                    func.cast(
                        Signal.result == "WIN",
                        Integer,
                    )
                ).label("wins"),
                func.sum(
                    func.cast(
                        Signal.result == "LOSS",
                        Integer,
                    )
                ).label("losses"),
            )
            .where(
                Signal.result.in_(
                    [
                        "WIN",
                        "LOSS",
                    ]
                )
            )
            .group_by(
                Signal.pair
            )
            .order_by(
                Signal.pair
            )
        )

        rows = result.all()

        stats = []

        for row in rows:

            wins = int(
                row.wins or 0
            )

            losses = int(
                row.losses or 0
            )

            decided = (
                wins + losses
            )

            winrate = (
                wins / decided * 100.0
                if decided
                else None
            )

            stats.append(
                {
                    "pair": row.pair,
                    "total": int(
                        row.total or 0
                    ),
                    "wins": wins,
                    "losses": losses,
                    "winrate": winrate,
                }
            )

        return stats


# ============================================================
# RECENT
# ============================================================

async def get_recent_signals(
    limit: int = 20,
):

    limit = max(
        1,
        min(
            int(limit),
            100,
        ),
    )

    async with Session() as session:

        result = await session.execute(
            select(Signal)
            .order_by(
                Signal.entry_time.desc()
            )
            .limit(limit)
        )

        return list(
            result.scalars().all()
        )


# ============================================================
# USER STATS
# ============================================================

async def get_user_stats():

    async with Session() as session:

        users = (
            await session.scalar(
                select(
                    func.count(
                        User.id
                    )
                )
            )
            or 0
        )

        active = (
            await session.scalar(
                select(
                    func.count(
                        User.id
                    )
                ).where(
                    User.access.is_(True),
                    User.blocked.is_(False),
                )
            )
            or 0
        )

        blocked = (
            await session.scalar(
                select(
                    func.count(
                        User.id
                    )
                ).where(
                    User.blocked.is_(True)
                )
            )
            or 0
        )

        return {
            "users": int(users),
            "active": int(active),
            "blocked": int(blocked),
        }


# ============================================================
# EXPIRED PENDING
# ============================================================

async def mark_expired_signals():

    # В LOSS автоматически не превращаем.
    # Нужна фактическая цена закрытия.

    now = datetime.now(
        timezone.utc
    )

    async with Session() as session:

        result = await session.execute(
            select(Signal)
            .where(
                Signal.status == "PENDING",
                Signal.close_time <= now,
            )
        )

        return list(
            result.scalars().all()
        )
