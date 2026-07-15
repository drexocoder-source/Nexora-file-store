"""SQLAlchemy models for Nexora File Store (Neon Postgres)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bots: Mapped[list["Bot"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"))
    bot_token: Mapped[str] = mapped_column(Text, unique=True)
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bot_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    welcome_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    welcome_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_channel: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped["Owner"] = relationship(back_populates="bots")
    channels: Mapped[list["BotChannel"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    folder_links: Mapped[list["FolderLink"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    files: Mapped[list["UploadedFile"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    users: Mapped[list["CloneUser"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    broadcasts: Mapped[list["Broadcast"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    settings: Mapped["BotSettings"] = relationship(
        back_populates="bot", uselist=False, cascade="all, delete-orphan"
    )


class BotChannel(Base):
    __tablename__ = "bot_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="channel")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    bot: Mapped["Bot"] = relationship(back_populates="channels")


class FolderLink(Base):
    __tablename__ = "folder_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    invite_link: Mapped[str] = mapped_column(Text)

    bot: Mapped["Bot"] = relationship(back_populates="folder_links")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    file_unique_id: Mapped[str] = mapped_column(String(128))
    file_id: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(32))
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bot: Mapped["Bot"] = relationship(back_populates="files")


class CloneUser(Base):
    __tablename__ = "clone_users"
    __table_args__ = (UniqueConstraint("bot_id", "user_id", name="uq_clone_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bot: Mapped["Bot"] = relationship(back_populates="users")


class JoinLog(Base):
    __tablename__ = "join_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)

    bot: Mapped["Bot"] = relationship(back_populates="broadcasts")


class BotSettings(Base):
    __tablename__ = "bot_settings"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)
    auto_delete: Mapped[int] = mapped_column(Integer, default=0)
    protect_content: Mapped[bool] = mapped_column(Boolean, default=False)
    send_files_once: Mapped[bool] = mapped_column(Boolean, default=False)
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    force_subscribe: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_button: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_photo: Mapped[str | None] = mapped_column(Text, nullable=True)

    bot: Mapped["Bot"] = relationship(back_populates="settings")


class OwnerLog(Base):
    __tablename__ = "owner_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(Text)
    time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
