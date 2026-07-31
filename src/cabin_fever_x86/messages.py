"""Wire types exchanged between the clients and the server.

Every message carries a ``type`` discriminator so the receiving side can tell
them apart, and an ``id`` so a single transmission can be traced through both
sides' logs.

A connection starts with the client issuing one of the session commands —
:class:`NewGameCommand`, :class:`ResumeGameCommand`, or
:class:`ListSessionsCommand`. The server answers each with a result carrying
the ``request_id`` of the command it answers. No game exists, and no
:class:`UserMessage` is accepted, until ``new_game`` or ``resume_game``
succeeds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter

# --- Client -> server -------------------------------------------------------


class UserMessage(BaseModel):
    """A transmission from the player."""

    type: Literal["user"] = "user"
    id: UUID = Field(default_factory=uuid4)
    content: str


class NewGameCommand(BaseModel):
    """Ask the server to start a fresh session."""

    type: Literal["new_game"] = "new_game"
    id: UUID = Field(default_factory=uuid4)


class ResumeGameCommand(BaseModel):
    """Ask the server to pick an existing session back up."""

    type: Literal["resume_game"] = "resume_game"
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID


class ListSessionsCommand(BaseModel):
    """Ask the server which sessions it holds data for."""

    type: Literal["list_sessions"] = "list_sessions"
    id: UUID = Field(default_factory=uuid4)


ClientMessage = Annotated[
    UserMessage | NewGameCommand | ResumeGameCommand | ListSessionsCommand,
    Field(discriminator="type"),
]

SessionCommand = NewGameCommand | ResumeGameCommand | ListSessionsCommand

# --- Server -> client -------------------------------------------------------


class AssistantMessage(BaseModel):
    """A transmission from the companion on the other end of the channel."""

    type: Literal["assistant"] = "assistant"
    id: UUID = Field(default_factory=uuid4)
    content: str


class SessionResult(BaseModel):
    """The session a ``new_game`` or ``resume_game`` command opened.

    The client and the server may be on different machines, each keeping its
    own ``data/sessions/<session_id>/`` directory. Sharing the id is what makes
    the two sets of logs line up afterwards.
    """

    type: Literal["session"] = "session"
    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    session_id: UUID


class SessionInfo(BaseModel):
    """One entry in a ``list_sessions`` result."""

    session_id: UUID
    modified: datetime


class SessionListResult(BaseModel):
    """The sessions the server holds data for, most recent first."""

    type: Literal["session_list"] = "session_list"
    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    sessions: list[SessionInfo] = Field(default_factory=list)


class ErrorResult(BaseModel):
    """A command the server could not carry out."""

    type: Literal["error"] = "error"
    id: UUID = Field(default_factory=uuid4)
    request_id: UUID | None = None
    message: str


ServerMessage = Annotated[
    AssistantMessage | SessionResult | SessionListResult | ErrorResult,
    Field(discriminator="type"),
]

CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
SERVER_MESSAGE_ADAPTER: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
