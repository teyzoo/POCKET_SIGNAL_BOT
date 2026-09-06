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
# DATABASE ENGINE
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

    # --------------------------------------------------------
    # LAST SEEN
    # --------------------------------------------------------
    #
    # В текущей PostgreSQL базе эта колонка уже существует
    # и имеет NOT NULL.
    #
    # Раньше модель User её не содержала, из-за чего INSERT
    # нового пользователя завершался:
    #
    # NotNullViolationError:
    # null value in column "last_seen"
    #
    # Теперь поле есть и будет автоматически обновляться
    # при каждом вызове ensure_user().
    #

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
    )

    timeframe: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(
        String(8),
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
        default="PENDING",
        nullable=False,
    )

    result: Mapped[Optional[str]] = mapped_column(
        String(16),
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

        # ----------------------------------------------------
        # CREATE TABLES
        # ----------------------------------------------------

        await connection.run_sync(
            Base.metadata.create_all
        )

        # ----------------------------------------------------
        # POSTGRESQL MIGRATIONS
        # ----------------------------------------------------

        if "postgresql" in config.database_url:

            # =================================================
            # TELEGRAM ID -> BIGINT
            # =================================================

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
                    "[DATABASE] Не удалось проверить "
                    "telegram_id: %s",
                    exc,
                )

            # =================================================
            # LAST_SEEN
            # =================================================

            try:

                # ------------------------------------------------
                # Проверяем, существует ли last_seen
                # ------------------------------------------------

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

                # ------------------------------------------------
                # Если колонки нет — создаём
                # ------------------------------------------------

                if row is None:

                    logger.warning(
                        "[DATABASE] Колонка users.last_seen "
                        "отсутствует. Создаю..."
                    )

                    await connection.exec_driver_sql(
                        """
                        ALTER TABLE users
                        ADD COLUMN last_seen
                        TIMESTAMP WITH TIME ZONE;
                        """
                    )

                    # --------------------------------------------
                    # Заполняем существующие записи
                    # --------------------------------------------

                    await connection.exec_driver_sql(
                        """
                        UPDATE users
                        SET last_seen = COALESCE(
                            created_at,
                            NOW()
                        )
                        WHERE last_seen IS NULL;
                        """
                    )

                    # --------------------------------------------
                    # Устанавливаем default
                    # --------------------------------------------

                    await connection.exec_driver_sql(
                        """
                        ALTER TABLE users
                        ALTER COLUMN last_seen
                        SET DEFAULT NOW();
                        """
                    )

                    # --------------------------------------------
                    # Теперь делаем NOT NULL
                    # --------------------------------------------

                    await connection.exec_driver_sql(
                        """
                        ALTER TABLE users
                        ALTER COLUMN last_seen
                        SET NOT NULL;
                        """
                    )

                    logger.info(
                        "[DATABASE] ✅ users.last_seen "
                        "создан и настроен."
                    )

                else:

                    # ------------------------------------------------
                    # Колонка уже существует
                    # ------------------------------------------------

                    logger.info(
                        "[DATABASE] users.last_seen "
                        "уже существует."
                    )

                    # --------------------------------------------
                    # Исправляем NULL, если такие есть
                    # --------------------------------------------

                    await connection.exec_driver_sql(
                        """
                        UPDATE users
                        SET last_seen = COALESCE(
                            created_at,
                            NOW()
                        )
                        WHERE last_seen IS NULL;
                        """
                    )

                    # --------------------------------------------
                    # Ставим DEFAULT
                    # --------------------------------------------

                    await connection.exec_driver_sql(
                        """
                        ALTER TABLE users
                        ALTER COLUMN last_seen
                        SET DEFAULT NOW();
                        """
                    )

                    # --------------------------------------------
                    # Убеждаемся, что NOT NULL установлен
                    # --------------------------------------------

                    if row.is_nullable == "YES":

                        await connection.exec_driver_sql(
                            """
                            ALTER TABLE users
                            ALTER COLUMN last_seen
                            SET NOT NULL;
                            """
                        )

                        logger.info(
                            "[DATABASE] "
                            "users.last_seen установлен "
                            "как NOT NULL."
                        )

            except Exception as exc:

                logger.exception(
                    "[DATABASE] ❌ Ошибка миграции "
                    "users.last_seen: %s",
                    exc,
                )

                raise

    logger.info(
        "[DATABASE] ✅ PostgreSQL готов."
    )


# ============================================================
# ENSURE USER
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

        # ====================================================
        # NEW USER
        # ====================================================

        if user is None:

            logger.info(
                "[DATABASE] Создаю нового пользователя "
                "telegram_id=%s",
                telegram_id,
            )

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

        # ====================================================
        # EXISTING USER
        # ====================================================

        else:

            user.username = username
            user.first_name = first_name
            user.last_seen = now

        await session.commit()

        return user


# ============================================================
# GET USER
# ============================================================

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


# ============================================================
# UPDATE USER
# ============================================================

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

            if hasattr(
                user,
                key,
            ):

                setattr(
                    user,
                    key,
                    value,
                )

        # Обновляем last_seen при изменении пользователя
        user.last_seen = datetime.now(
            timezone.utc
        )

        await session.commit()


# ============================================================
# GET ACCESS USERS
# ============================================================

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


# ============================================================
# MARK EXPIRED SIGNALS
# ============================================================

async def mark_expired_signals():

    """
    Переводит истёкшие PENDING сигналы в EXPIRED.

    WIN/LOSS здесь не определяется.
    Для WIN/LOSS необходимо сравнение направления
    сигнала с фактической ценой закрытия.
    """

    now = datetime.now(
        timezone.utc
    )

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


# ============================================================
# SET SIGNAL RESULT
# ============================================================

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


# ============================================================
# SIGNAL STATS
# ============================================================

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

        decided = (
            wins + losses
        )

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


# ============================================================
# USER STATS
# ============================================================

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


# ============================================================
# RECENT SIGNALS
# ============================================================

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


# ============================================================
# PAIR STATS
# ============================================================

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


# ============================================================
# CLOSE DATABASE
# ============================================================

async def close_database():

    await engine.dispose()

    logger.info(
        "[DATABASE] Database connection closed."
    )
