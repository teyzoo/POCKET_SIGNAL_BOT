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


logger = logging.getLogger("pocket_database")


# ============================================================
# SETTINGS
# ============================================================

MIN_HISTORY_SAMPLE = 20


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
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
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

    # Технический score.
    # Это НЕ исторический WINRATE.
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

    # PENDING / WIN / LOSS / DRAW
    status: Mapped[str] = mapped_column(
        String(16),
        default="PENDING",
        nullable=False,
        index=True,
    )

    # WIN / LOSS / DRAW
    result: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        index=True,
    )

    # Цена в момент входа.
    entry_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    # Цена на момент экспирации.
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
# INIT DATABASE
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

            # ------------------------------------------------
            # TELEGRAM ID
            # ------------------------------------------------

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

            # ------------------------------------------------
            # LAST SEEN
            # ------------------------------------------------

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

            # ------------------------------------------------
            # SIGNAL PRICES
            # ------------------------------------------------

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
# CLOSE DATABASE
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
# SAVE SIGNAL
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
            pair=str(pair),
            timeframe=int(timeframe),
            direction=str(direction).upper(),
            probability=float(probability),
            quality=float(quality),
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
                reasons or [],
                ensure_ascii=False,
            ),
        )

        session.add(signal)

        await session.commit()

        await session.refresh(signal)

        logger.info(
            "[SIGNAL] Saved id=%s pair=%s tf=%s direction=%s entry=%s close=%s price=%s",
            signal.id,
            signal.pair,
            signal.timeframe,
            signal.direction,
            signal.entry_time,
            signal.close_time,
            signal.entry_price,
        )

        return signal.id


# ============================================================
# PENDING SIGNALS
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
# SET RESULT
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
        "DRAW",
    }:

        raise ValueError(
            "Результат должен быть WIN, LOSS или DRAW."
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
            "DRAW",
        }:

            return False

        signal.result = result
        signal.status = result

        if close_price is not None:

            signal.close_price = float(
                close_price
            )

        await session.commit()

        logger.info(
            "[RESULT] signal=%s result=%s close_price=%s",
            signal_id,
            result,
            close_price,
        )

        return True


# ============================================================
# SETTLE BY PRICE
# ============================================================

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

            logger.warning(
                "[RESULT] signal=%s has no entry_price",
                signal_id,
            )

            return None

        entry_price = float(
            signal.entry_price
        )

        close_price = float(
            close_price
        )

        # ----------------------------------------------------
        # DRAW
        # ----------------------------------------------------

        if close_price == entry_price:

            signal.result = "DRAW"
            signal.status = "DRAW"
            signal.close_price = close_price

            await session.commit()

            logger.info(
                "[RESULT] signal=%s DRAW entry=%s close=%s",
                signal_id,
                entry_price,
                close_price,
            )

            return "DRAW"

        # ----------------------------------------------------
        # DIRECTION
        # ----------------------------------------------------

        direction = str(
            signal.direction
        ).upper()

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

            logger.warning(
                "[RESULT] signal=%s unknown direction=%s",
                signal_id,
                direction,
            )

            return None

        signal.result = result
        signal.status = result
        signal.close_price = close_price

        await session.commit()

        logger.info(
            "[RESULT] signal=%s %s entry=%s close=%s direction=%s",
            signal_id,
            result,
            entry_price,
            close_price,
            direction,
        )

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

        draws = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.result == "DRAW"
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
            "draws": int(draws),
            "decided": int(decided),
            "winrate": winrate,
            "reliable": (
                decided >= MIN_HISTORY_SAMPLE
            ),
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

                func.sum(
                    func.cast(
                        Signal.result == "DRAW",
                        Integer,
                    )
                ).label("draws"),
            )
            .where(
                Signal.result.in_(
                    [
                        "WIN",
                        "LOSS",
                        "DRAW",
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

            draws = int(
                row.draws or 0
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
                    "draws": draws,
                    "decided": decided,
                    "winrate": winrate,
                    "reliable": (
                        decided
                        >= MIN_HISTORY_SAMPLE
                    ),
                }
            )

        return stats


# ============================================================
# DETAILED STATS
# PAIR + TIMEFRAME + DIRECTION
# ============================================================

async def get_signal_profile_stats(
    pair: str,
    timeframe: int,
    direction: str,
):

    direction = str(
        direction
    ).upper()

    async with Session() as session:

        wins = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.pair == pair,
                    Signal.timeframe == int(timeframe),
                    Signal.direction == direction,
                    Signal.result == "WIN",
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
                    Signal.pair == pair,
                    Signal.timeframe == int(timeframe),
                    Signal.direction == direction,
                    Signal.result == "LOSS",
                )
            )
            or 0
        )

        draws = (
            await session.scalar(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.pair == pair,
                    Signal.timeframe == int(timeframe),
                    Signal.direction == direction,
                    Signal.result == "DRAW",
                )
            )
            or 0
        )

        wins = int(wins)
        losses = int(losses)
        draws = int(draws)

        decided = (
            wins + losses
        )

        winrate = (
            wins / decided * 100.0
            if decided
            else None
        )

        return {
            "pair": pair,
            "timeframe": int(timeframe),
            "direction": direction,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "decided": decided,
            "winrate": winrate,
            "reliable": (
                decided
                >= MIN_HISTORY_SAMPLE
            ),
        }


# ============================================================
# ALL PROFILE STATS
# ============================================================

async def get_all_profile_stats():

    async with Session() as session:

        result = await session.execute(
            select(
                Signal.pair,
                Signal.timeframe,
                Signal.direction,

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

                func.sum(
                    func.cast(
                        Signal.result == "DRAW",
                        Integer,
                    )
                ).label("draws"),
            )
            .where(
                Signal.result.in_(
                    [
                        "WIN",
                        "LOSS",
                        "DRAW",
                    ]
                )
            )
            .group_by(
                Signal.pair,
                Signal.timeframe,
                Signal.direction,
            )
            .order_by(
                Signal.pair,
                Signal.timeframe,
                Signal.direction,
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

            draws = int(
                row.draws or 0
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
                    "timeframe": int(
                        row.timeframe
                    ),
                    "direction": str(
                        row.direction
                    ).upper(),
                    "wins": wins,
                    "losses": losses,
                    "draws": draws,
                    "decided": decided,
                    "winrate": winrate,
                    "reliable": (
                        decided
                        >= MIN_HISTORY_SAMPLE
                    ),
                }
            )

        return stats


# ============================================================
# RECENT SIGNALS
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
