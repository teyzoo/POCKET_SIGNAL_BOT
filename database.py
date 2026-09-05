from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    case,
    func,
    select,
    text,
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


# ============================================================
# DATABASE URL
# ============================================================

def normalize_database_url(url: str) -> str:

    if not url:
        raise RuntimeError(
            "DATABASE_URL is empty"
        )

    if url.startswith("postgres://"):

        return url.replace(
            "postgres://",
            "postgresql+asyncpg://",
            1,
        )

    if url.startswith("postgresql://"):

        return url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
            1,
        )

    if url.startswith(
        "postgresql+psycopg2://"
    ):

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


DATABASE_URL = normalize_database_url(
    config.database_url
)


# ============================================================
# ENGINE
# ============================================================

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
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
# UTC
# ============================================================

def utc_now() -> datetime:
    """
    Текущее время в UTC.
    """

    return datetime.now(
        timezone.utc
    )


def ensure_utc(
    value: datetime,
) -> datetime:
    """
    Приводит datetime к UTC.
    """

    if value.tzinfo is None:

        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


# ============================================================
# USER
# ============================================================

class User(Base):

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # ВАЖНО:
    # Telegram ID должен быть BIGINT.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )

    username: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    first_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    access: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    blocked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    pair: Mapped[str] = mapped_column(
        String(32),
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


# ============================================================
# JOIN REQUEST
# ============================================================

class JoinRequest(Base):

    __tablename__ = "join_requests"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # Telegram ID тоже BIGINT.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    username: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    first_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
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
        String(32),
        index=True,
        nullable=False,
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

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
    )

    close_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="ACTIVE",
        nullable=False,
    )

    result: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    reasons: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


# ============================================================
# SETTINGS
# ============================================================

class Setting(Base):

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    key: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

async def init_db():

    async with engine.begin() as conn:

        # ----------------------------------------------------
        # Создание отсутствующих таблиц
        # ----------------------------------------------------

        await conn.run_sync(
            Base.metadata.create_all
        )

        # ----------------------------------------------------
        # MIGRATION
        #
        # Старые версии бота создавали telegram_id
        # как INTEGER.
        #
        # Telegram ID пользователя:
        #
        # 7364836929
        #
        # больше максимального INTEGER:
        #
        # 2147483647
        #
        # Поэтому переводим на BIGINT.
        # ----------------------------------------------------

        if conn.dialect.name == "postgresql":

            await conn.execute(
                text(
                    """
                    ALTER TABLE users
                    ALTER COLUMN telegram_id
                    TYPE BIGINT
                    USING telegram_id::BIGINT
                    """
                )
            )

            await conn.execute(
                text(
                    """
                    ALTER TABLE join_requests
                    ALTER COLUMN telegram_id
                    TYPE BIGINT
                    USING telegram_id::BIGINT
                    """
                )
            )

        # ----------------------------------------------------
        # COMMIT
        # ----------------------------------------------------

        await conn.commit()


# ============================================================
# GET USER
# ============================================================

async def get_user(
    telegram_id: int,
) -> User | None:

    async with Session() as session:

        result = await session.execute(
            select(User).where(
                User.telegram_id == telegram_id
            )
        )

        return result.scalar_one_or_none()


# ============================================================
# ENSURE USER
# ============================================================

async def ensure_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:

    async with Session() as session:

        result = await session.execute(
            select(User).where(
                User.telegram_id == telegram_id
            )
        )

        user = result.scalar_one_or_none()

        # ----------------------------------------------------
        # NEW USER
        # ----------------------------------------------------

        if user is None:

            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                access=(
                    telegram_id
                    == config.owner_id
                ),
                blocked=False,
                pair="ANY",
                timeframe=5,
                auto_signals=True,
                created_at=utc_now(),
                last_seen=utc_now(),
            )

            session.add(user)

        # ----------------------------------------------------
        # EXISTING USER
        # ----------------------------------------------------

        else:

            user.username = username

            user.first_name = first_name

            user.last_seen = utc_now()

            # Владелец всегда получает доступ.
            if telegram_id == config.owner_id:

                user.access = True
                user.blocked = False

        await session.commit()

        return user


# ============================================================
# UPDATE USER
# ============================================================

async def update_user(
    telegram_id: int,
    **kwargs: Any,
):

    async with Session() as session:

        result = await session.execute(
            select(User).where(
                User.telegram_id == telegram_id
            )
        )

        user = result.scalar_one_or_none()

        if user is None:
            return

        for key, value in kwargs.items():

            if hasattr(user, key):

                setattr(
                    user,
                    key,
                    value,
                )

        user.last_seen = utc_now()

        await session.commit()


# ============================================================
# SAVE JOIN REQUEST
# ============================================================

async def save_join_request(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
):

    async with Session() as session:

        result = await session.execute(
            select(JoinRequest).where(
                JoinRequest.telegram_id
                == telegram_id,
                JoinRequest.status
                == "pending",
            )
        )

        existing = (
            result.scalar_one_or_none()
        )

        if existing:

            return existing

        request = JoinRequest(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            status="pending",
            created_at=utc_now(),
        )

        session.add(request)

        await session.commit()

        return request


# ============================================================
# SET JOIN REQUEST STATUS
# ============================================================

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


# ============================================================
# GET PENDING REQUESTS
# ============================================================

async def get_pending_requests():

    async with Session() as session:

        result = await session.execute(
            select(JoinRequest)
            .where(
                JoinRequest.status
                == "pending"
            )
            .order_by(
                JoinRequest.created_at.desc()
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
):

    entry_time = ensure_utc(
        entry_time
    )

    close_time = ensure_utc(
        close_time
    )

    async with Session() as session:

        signal = Signal(
            pair=pair,
            timeframe=timeframe,
            direction=direction,
            probability=float(
                probability
            ),
            quality=float(
                quality
            ),
            entry_time=entry_time,
            close_time=close_time,
            status="ACTIVE",
            result=None,
            reasons=" | ".join(
                reasons
            ),
            created_at=utc_now(),
        )

        session.add(signal)

        await session.commit()

        return signal


# ============================================================
# GET RECENT SIGNALS
# ============================================================

async def get_recent_signals(
    limit: int = 10,
):

    async with Session() as session:

        result = await session.execute(
            select(Signal)
            .order_by(
                Signal.created_at.desc()
            )
            .limit(limit)
        )

        return list(
            result.scalars().all()
        )


# ============================================================
# GET SIGNAL STATS
# ============================================================

async def get_signal_stats():

    async with Session() as session:

        total = (
            await session.execute(
                select(
                    func.count(
                        Signal.id
                    )
                )
            )
        ).scalar() or 0

        wins = (
            await session.execute(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.result == "WIN"
                )
            )
        ).scalar() or 0

        losses = (
            await session.execute(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.result == "LOSS"
                )
            )
        ).scalar() or 0

        active = (
            await session.execute(
                select(
                    func.count(
                        Signal.id
                    )
                ).where(
                    Signal.status
                    == "ACTIVE"
                )
            )
        ).scalar() or 0

        completed = (
            wins + losses
        )

        if completed:

            winrate = (
                wins
                / completed
                * 100
            )

        else:

            winrate = 0.0

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "active": active,
            "winrate": winrate,
        }


# ============================================================
# GET USER STATS
# ============================================================

async def get_user_stats():

    async with Session() as session:

        total = (
            await session.execute(
                select(
                    func.count(
                        User.id
                    )
                )
            )
        ).scalar() or 0

        active = (
            await session.execute(
                select(
                    func.count(
                        User.id
                    )
                ).where(
                    User.access.is_(True),
                    User.blocked.is_(False),
                )
            )
        ).scalar() or 0

        blocked = (
            await session.execute(
                select(
                    func.count(
                        User.id
                    )
                ).where(
                    User.blocked.is_(True)
                )
            )
        ).scalar() or 0

        return {
            "total": total,
            "active": active,
            "blocked": blocked,
        }


# ============================================================
# GET USERS WITH ACCESS
# ============================================================

async def get_access_users():

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
# GET PAIR STATS
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
                    case(
                        (
                            Signal.result
                            == "WIN",
                            1,
                        ),
                        else_=0,
                    )
                ).label("wins"),

                func.sum(
                    case(
                        (
                            Signal.result
                            == "LOSS",
                            1,
                        ),
                        else_=0,
                    )
                ).label("losses"),
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


# ============================================================
# SETTING
# ============================================================

async def set_setting(
    key: str,
    value: str,
):

    async with Session() as session:

        result = await session.execute(
            select(Setting).where(
                Setting.key == key
            )
        )

        setting = (
            result.scalar_one_or_none()
        )

        if setting:

            setting.value = value

        else:

            setting = Setting(
                key=key,
                value=value,
            )

            session.add(setting)

        await session.commit()


# ============================================================
# GET SETTING
# ============================================================

async def get_setting(
    key: str,
    default: str | None = None,
):

    async with Session() as session:

        result = await session.execute(
            select(Setting).where(
                Setting.key == key
            )
        )

        setting = (
            result.scalar_one_or_none()
        )

        if setting:

            return setting.value

        return default


# ============================================================
# GET EXPIRED SIGNALS
# ============================================================

async def get_expired_signals():

    now = utc_now()

    async with Session() as session:

        result = await session.execute(
            select(Signal).where(
                Signal.status == "ACTIVE",
                Signal.close_time <= now,
            )
        )

        return list(
            result.scalars().all()
        )


# ============================================================
# MARK EXPIRED SIGNALS
# ============================================================

async def mark_expired_signals():

    now = utc_now()

    async with Session() as session:

        result = await session.execute(
            select(Signal).where(
                Signal.status == "ACTIVE",
                Signal.close_time <= now,
            )
        )

        signals = list(
            result.scalars().all()
        )

        for signal in signals:

            signal.status = "EXPIRED"

        await session.commit()

        return signals


# ============================================================
# UPDATE SIGNAL RESULT
# ============================================================

async def update_signal_result(
    signal_id: int,
    result_value: str,
):

    result_value = result_value.upper()

    if result_value not in {
        "WIN",
        "LOSS",
        "DRAW",
    }:

        raise ValueError(
            "Invalid signal result"
        )

    async with Session() as session:

        signal = await session.get(
            Signal,
            signal_id,
        )

        if signal is None:
            return None

        signal.result = result_value

        signal.status = (
            "COMPLETED"
        )

        await session.commit()

        return signal


# ============================================================
# CLOSE DATABASE
# ============================================================

async def close_database():

    await engine.dispose()
