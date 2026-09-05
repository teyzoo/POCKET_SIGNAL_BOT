from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    select,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import config


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)

    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)

    if url.startswith("postgresql+psycopg2://"):
        return url.replace(
            "postgresql+psycopg2://",
            "postgresql+asyncpg://",
            1,
        )

    if url.startswith("sqlite:///"):
        return url.replace(
            "sqlite:///",
            "sqlite+aiosqlite:///",
            1,
        )

    return url


DATABASE_URL = normalize_database_url(config.database_url)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

Session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(
        Integer,
        unique=True,
        index=True,
    )

    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))

    access: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    pair: Mapped[str] = mapped_column(
        String(32),
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )

    last_seen: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )


class JoinRequest(Base):
    __tablename__ = "join_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    telegram_id: Mapped[int] = mapped_column(
        Integer,
        index=True,
    )

    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    pair: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[int] = mapped_column(Integer)

    direction: Mapped[str] = mapped_column(String(16))
    probability: Mapped[float] = mapped_column(Float)
    quality: Mapped[float] = mapped_column(Float)

    entry_time: Mapped[datetime] = mapped_column(DateTime)
    close_time: Mapped[datetime] = mapped_column(DateTime)

    status: Mapped[str] = mapped_column(
        String(16),
        default="ACTIVE",
    )

    result: Mapped[str | None] = mapped_column(String(16))

    reasons: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    key: Mapped[str] = mapped_column(
        String(128),
        unique=True,
    )

    value: Mapped[str] = mapped_column(Text)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_user(telegram_id: int) -> User | None:
    async with Session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def ensure_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:
    async with Session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                access=(telegram_id == config.owner_id),
            )
            session.add(user)
        else:
            user.username = username
            user.first_name = first_name
            user.last_seen = datetime.now(timezone.utc)

        await session.commit()
        return user


async def update_user(
    telegram_id: int,
    **kwargs: Any,
):
    async with Session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            return

        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)

        await session.commit()


async def save_join_request(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
):
    async with Session() as session:
        result = await session.execute(
            select(JoinRequest).where(
                JoinRequest.telegram_id == telegram_id,
                JoinRequest.status == "pending",
            )
        )

        existing = result.scalar_one_or_none()

        if existing:
            return existing

        request = JoinRequest(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )

        session.add(request)
        await session.commit()

        return request


async def set_join_request_status(
    request_id: int,
    status: str,
):
    async with Session() as session:
        request = await session.get(
            JoinRequest,
            request_id,
        )

        if request:
            request.status = status
            await session.commit()


async def get_pending_requests():
    async with Session() as session:
        result = await session.execute(
            select(JoinRequest)
            .where(JoinRequest.status == "pending")
            .order_by(JoinRequest.created_at.desc())
        )
        return list(result.scalars().all())


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
            reasons=" | ".join(reasons),
        )

        session.add(signal)
        await session.commit()

        return signal


async def get_recent_signals(limit: int = 10):
    async with Session() as session:
        result = await session.execute(
            select(Signal)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_signal_stats():
    async with Session() as session:
        total = (
            await session.execute(
                select(func.count(Signal.id))
            )
        ).scalar() or 0

        wins = (
            await session.execute(
                select(func.count(Signal.id))
                .where(Signal.result == "WIN")
            )
        ).scalar() or 0

        losses = (
            await session.execute(
                select(func.count(Signal.id))
                .where(Signal.result == "LOSS")
            )
        ).scalar() or 0

        active = (
            await session.execute(
                select(func.count(Signal.id))
                .where(Signal.status == "ACTIVE")
            )
        ).scalar() or 0

        completed = wins + losses
        winrate = (
            (wins / completed) * 100
            if completed
            else 0
        )

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "active": active,
            "winrate": winrate,
        }


async def get_user_stats():
    async with Session() as session:
        total = (
            await session.execute(
                select(func.count(User.id))
            )
        ).scalar() or 0

        active = (
            await session.execute(
                select(func.count(User.id))
                .where(
                    User.access.is_(True),
                    User.blocked.is_(False),
                )
            )
        ).scalar() or 0

        blocked = (
            await session.execute(
                select(func.count(User.id))
                .where(User.blocked.is_(True))
            )
        ).scalar() or 0

        return {
            "total": total,
            "active": active,
            "blocked": blocked,
        }


async def get_access_users():
    async with Session() as session:
        result = await session.execute(
            select(User).where(
                User.access.is_(True),
                User.blocked.is_(False),
            )
        )

        return list(result.scalars().all())


async def get_pair_stats():
    async with Session() as session:
        result = await session.execute(
            select(
                Signal.pair,
                func.count(Signal.id),
                func.sum(
                    func.case(
                        (Signal.result == "WIN", 1),
                        else_=0,
                    )
                ),
                func.sum(
                    func.case(
                        (Signal.result == "LOSS", 1),
                        else_=0,
                    )
                ),
            )
            .group_by(Signal.pair)
            .order_by(func.count(Signal.id).desc())
        )

        return result.all()


async def set_setting(key: str, value: str):
    async with Session() as session:
        result = await session.execute(
            select(Setting).where(Setting.key == key)
        )

        setting = result.scalar_one_or_none()

        if setting:
            setting.value = value
        else:
            session.add(
                Setting(
                    key=key,
                    value=value,
                )
            )

        await session.commit()


async def get_setting(
    key: str,
    default: str | None = None,
):
    async with Session() as session:
        result = await session.execute(
            select(Setting).where(Setting.key == key)
        )

        setting = result.scalar_one_or_none()

        if setting:
            return setting.value

        return default


async def mark_expired_signals():
    async with Session() as session:
        now = datetime.now(timezone.utc)

        result = await session.execute(
            select(Signal).where(
                Signal.status == "ACTIVE",
                Signal.close_time <= now,
            )
        )

        signals = list(result.scalars().all())

        for signal in signals:
            signal.status = "EXPIRED"

        await session.commit()

        return signals
