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

MIN_HISTORY_SAMPLE = 20


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

    # Техническая уверенность.
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

    status: Mapped[str] = mapped_column(
        String(16),
        default="PENDING",
        nullable=False,
        index=True,
    )

    result: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        index=True,
    )

    entry_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    close_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    reasons: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )


async def init_db():
    logger.info("[DATABASE] Инициализация PostgreSQL...")

    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all
        )

        if "postgresql" in config.database_url:
            try:
                await connection.exec_driver_sql(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS last_seen
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
                logger.warning(
                    "[DATABASE] users migration: %s",
                    exc,
                )

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
                logger.warning(
                    "[DATABASE] signals migration: %s",
                    exc,
                )

    logger.info("[DATABASE] ✅ PostgreSQL готов")


async def close_database():
    await engine.dispose()
    logger.info("[DATABASE] Соединения закрыты")


async def ensure_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:

    now = datetime.now(timezone.utc)

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
            return False

        for key, value in values.items():
            if hasattr(user, key):
                setattr(user, key, value)

        user.last_seen = datetime.now(
            timezone.utc
        )

        await session.commit()
        return True


async def get_access_users() -> list[User]:

    async with Session() as session:
        result = await session.execute(
            select(User).where(
                User.access.is_(True),
                User.blocked.is_(False),
            )
        )

        return list(result.scalars().all())


async def save_signal(
    pair=None,
    timeframe=None,
    direction=None,
    probability=None,
    quality=None,
    entry_time=None,
    close_time=None,
    reasons=None,
    entry_price=None,
):
    """
    Поддерживает:

        save_signal(signal)

    и:

        save_signal(
            pair,
            timeframe,
            direction,
            probability,
            quality,
            entry_time,
            close_time,
            reasons,
            entry_price
        )
    """

    if (
        pair is not None
        and not isinstance(pair, str)
        and timeframe is None
    ):
        obj = pair

        pair = getattr(obj, "pair", None)
        timeframe = getattr(obj, "timeframe", None)
        direction = getattr(obj, "direction", None)
        probability = getattr(obj, "probability", None)
        quality = getattr(obj, "quality", None)
        entry_time = getattr(obj, "entry_time", None)
        close_time = getattr(obj, "close_time", None)
        reasons = getattr(obj, "reasons", None)
        entry_price = getattr(obj, "entry_price", None)

    required = {
        "pair": pair,
        "timeframe": timeframe,
        "direction": direction,
        "probability": probability,
        "quality": quality,
        "entry_time": entry_time,
        "close_time": close_time,
    }

    for name, value in required.items():
        if value is None:
            raise ValueError(
                f"save_signal: {name} отсутствует"
            )

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
            "[SIGNAL] Saved id=%s pair=%s tf=%s "
            "direction=%s entry=%s close=%s price=%s",
            signal.id,
            signal.pair,
            signal.timeframe,
            signal.direction,
            signal.entry_time,
            signal.close_time,
            signal.entry_price,
        )

        return signal.id


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

        return list(result.scalars().all())


async def get_signal(
    signal_id: int,
) -> Signal | None:

    async with Session() as session:
        return await session.get(
            Signal,
            signal_id,
        )


async def set_signal_result(
    signal_id: int,
    result: str,
    close_price: float | None = None,
):

    result = str(result).upper().strip()

    if result not in {
        "WIN",
        "LOSS",
        "DRAW",
    }:
        raise ValueError(
            "Результат должен быть WIN, LOSS или DRAW"
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
            "[RESULT] id=%s result=%s close=%s",
            signal_id,
            result,
            close_price,
        )

        return True


async def settle_signal_by_price(
    signal_id: int,
    close_price: float,
):

    async with Session() as session:
        signal = await session.get(
            Signal,
            signal_id,
        )

        if signal is None:
            return None

        if signal.status != "PENDING":
            return signal.status

        if signal.entry_price is None:
            logger.warning(
                "[RESULT] id=%s нет entry_price",
                signal_id,
            )
            return None

        entry = float(signal.entry_price)
        close = float(close_price)

        if close == entry:
            result = "DRAW"

        elif signal.direction.upper() in {
            "UP",
            "CALL",
            "BUY",
        }:
            result = (
                "WIN"
                if close > entry
                else "LOSS"
            )

        else:
            result = (
                "WIN"
                if close < entry
                else "LOSS"
            )

        signal.close_price = close
        signal.result = result
        signal.status = result

        await session.commit()

        logger.info(
            "[SETTLE] id=%s %s entry=%s close=%s",
            signal_id,
            result,
            entry,
            close,
        )

        return result


async def get_signal_stats():

    async with Session() as session:
        wins = await session.scalar(
            select(func.count(Signal.id)).where(
                Signal.result == "WIN"
            )
        )

        losses = await session.scalar(
            select(func.count(Signal.id)).where(
                Signal.result == "LOSS"
            )
        )

        draws = await session.scalar(
            select(func.count(Signal.id)).where(
                Signal.result == "DRAW"
            )
        )

        wins = int(wins or 0)
        losses = int(losses or 0)
        draws = int(draws or 0)

        decided = wins + losses

        winrate = (
            wins / decided * 100
            if decided
            else 0.0
        )

        return {
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "decided": decided,
            "winrate": winrate,
            "reliable": decided >= MIN_HISTORY_SAMPLE,
        }


async def get_pair_stats():

    async with Session() as session:
        result = await session.execute(
            select(
                Signal.pair,
                func.count(Signal.id).label("total"),
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
                    ["WIN", "LOSS", "DRAW"]
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

    output = []

    for row in rows:
        wins = int(row.wins or 0)
        losses = int(row.losses or 0)
        draws = int(row.draws or 0)

        decided = wins + losses

        winrate = (
            wins / decided * 100
            if decided
            else 0.0
        )

        output.append({
            "pair": row.pair,
            "total": int(row.total or 0),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "decided": decided,
            "winrate": winrate,
            "reliable": (
                decided >= MIN_HISTORY_SAMPLE
            ),
        })

    return output


async def get_signal_profile_stats(
    pair: str,
    timeframe: int,
    direction: str,
):

    async with Session() as session:
        result = await session.execute(
            select(
                func.count(Signal.id).label("total"),
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
                Signal.pair == pair,
                Signal.timeframe == int(timeframe),
                Signal.direction == str(
                    direction
                ).upper(),
                Signal.result.in_(
                    ["WIN", "LOSS", "DRAW"]
                ),
            )
        )

        row = result.one()

    wins = int(row.wins or 0)
    losses = int(row.losses or 0)
    draws = int(row.draws or 0)

    decided = wins + losses

    return {
        "pair": pair,
        "timeframe": int(timeframe),
        "direction": str(direction).upper(),
        "total": int(row.total or 0),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "decided": decided,
        "winrate": (
            wins / decided * 100
            if decided
            else 0.0
        ),
        "reliable": (
            decided >= MIN_HISTORY_SAMPLE
        ),
    }


async def get_all_profile_stats():

    async with Session() as session:
        result = await session.execute(
            select(
                Signal.pair,
                Signal.timeframe,
                Signal.direction,
                func.count(Signal.id).label("total"),
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
                    ["WIN", "LOSS", "DRAW"]
                )
            )
            .group_by(
                Signal.pair,
                Signal.timeframe,
                Signal.direction,
            )
        )

        rows = result.all()

    output = []

    for row in rows:
        wins = int(row.wins or 0)
        losses = int(row.losses or 0)
        draws = int(row.draws or 0)

        decided = wins + losses

        output.append({
            "pair": row.pair,
            "timeframe": int(row.timeframe),
            "direction": row.direction,
            "total": int(row.total or 0),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "decided": decided,
            "winrate": (
                wins / decided * 100
                if decided
                else 0.0
            ),
            "reliable": (
                decided >= MIN_HISTORY_SAMPLE
            ),
        })

    return output
