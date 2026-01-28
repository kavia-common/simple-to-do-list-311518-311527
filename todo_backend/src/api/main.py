from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, Path, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ----------------------------
# OpenAPI metadata and tags
# ----------------------------
openapi_tags = [
    {
        "name": "Health",
        "description": "Service health and readiness endpoints.",
    },
    {
        "name": "Tasks",
        "description": "CRUD operations for to-do tasks.",
    },
]


app = FastAPI(
    title="To-Do Backend API",
    description=(
        "Backend service for a simple to-do app.\n\n"
        "It supports creating, listing, editing, completing and deleting tasks.\n\n"
        "Docs:\n"
        "- Swagger UI: `/docs`\n"
        "- OpenAPI schema: `/openapi.json`\n"
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# Persistence helpers (SQLite)
# ----------------------------
def _utc_now_iso() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _get_sqlite_path() -> str:
    """Return the configured SQLite DB file path.

    Note:
        Uses SQLITE_DB env var if present, otherwise defaults to a local file.
    """
    return os.getenv("SQLITE_DB", "todo.db")


def _get_conn() -> sqlite3.Connection:
    """Create a SQLite connection with row factory enabled."""
    conn = sqlite3.connect(_get_sqlite_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Initialize the tasks table if it does not exist."""
    conn = _get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


_init_db()


# ----------------------------
# Pydantic models (OpenAPI)
# ----------------------------
class TaskBase(BaseModel):
    """Common fields for tasks."""

    title: str = Field(..., min_length=1, max_length=200, description="Short title of the task.")
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Optional longer description of the task.",
    )


class TaskCreate(TaskBase):
    """Request body to create a new task."""

    completed: bool = Field(False, description="Initial completion status.")


class TaskUpdate(TaskBase):
    """Request body to fully replace a task (PUT)."""

    completed: bool = Field(..., description="Completion status.")


class TaskOut(TaskBase):
    """Task representation returned by the API."""

    id: int = Field(..., description="Unique task identifier.")
    completed: bool = Field(..., description="Whether the task is completed.")
    created_at: str = Field(..., description="ISO-8601 UTC timestamp for creation.")
    updated_at: str = Field(..., description="ISO-8601 UTC timestamp for last update.")


class TaskListResponse(BaseModel):
    """Response wrapper for listing tasks."""

    tasks: List[TaskOut] = Field(..., description="List of all tasks.")


class TaskCompleteResponse(BaseModel):
    """Response from the completion endpoint."""

    task: TaskOut = Field(..., description="The updated task.")


def _row_to_task(row: sqlite3.Row) -> TaskOut:
    """Convert a SQLite row to a TaskOut model."""
    return TaskOut(
        id=int(row["id"]),
        title=str(row["title"]),
        description=row["description"],
        completed=bool(row["completed"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


# ----------------------------
# Routes
# ----------------------------
@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Basic health check endpoint to verify the service is running.",
)
# PUBLIC_INTERFACE
def health_check():
    """Health check endpoint.

    Returns:
        JSON object indicating the backend is healthy.
    """
    return {"message": "Healthy"}


@app.get(
    "/tasks",
    response_model=TaskListResponse,
    tags=["Tasks"],
    summary="List tasks",
    description="Return all tasks ordered by id descending.",
    operation_id="list_tasks",
)
# PUBLIC_INTERFACE
def list_tasks() -> TaskListResponse:
    """List all tasks.

    Returns:
        TaskListResponse: All tasks currently stored.
    """
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC;").fetchall()
        return TaskListResponse(tasks=[_row_to_task(r) for r in rows])
    finally:
        conn.close()


@app.post(
    "/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Tasks"],
    summary="Create task",
    description="Create a new task and return the created object.",
    operation_id="create_task",
)
# PUBLIC_INTERFACE
def create_task(
    payload: TaskCreate = Body(
        ...,
        description="Task fields to create.",
        examples=[
            {"title": "Buy groceries", "description": "Milk, eggs, bread", "completed": False},
        ],
    )
) -> TaskOut:
    """Create a new task.

    Args:
        payload: TaskCreate request body.

    Returns:
        TaskOut: The created task.
    """
    now = _utc_now_iso()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO tasks (title, description, completed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                payload.title,
                payload.description,
                1 if payload.completed else 0,
                now,
                now,
            ),
        )
        conn.commit()
        task_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        assert row is not None
        return _row_to_task(row)
    finally:
        conn.close()


@app.put(
    "/tasks/{id}",
    response_model=TaskOut,
    tags=["Tasks"],
    summary="Replace task",
    description="Fully replace a task's fields (title/description/completed).",
    operation_id="replace_task",
)
# PUBLIC_INTERFACE
def replace_task(
    id: int = Path(..., ge=1, description="Task id."),
    payload: TaskUpdate = Body(
        ...,
        description="Full task fields to set.",
        examples=[
            {"title": "Buy groceries", "description": "Milk, eggs, bread", "completed": True},
        ],
    ),
) -> TaskOut:
    """Replace a task (PUT semantics).

    Args:
        id: Task id.
        payload: TaskUpdate request body.

    Returns:
        TaskOut: The updated task.

    Raises:
        HTTPException: 404 if the task is not found.
    """
    now = _utc_now_iso()
    conn = _get_conn()
    try:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?;", (id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Task not found")

        conn.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, completed = ?, updated_at = ?
            WHERE id = ?;
            """,
            (payload.title, payload.description, 1 if payload.completed else 0, now, id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (id,)).fetchone()
        assert row is not None
        return _row_to_task(row)
    finally:
        conn.close()


@app.patch(
    "/tasks/{id}/complete",
    response_model=TaskCompleteResponse,
    tags=["Tasks"],
    summary="Mark task complete/incomplete",
    description="Set the completion status of a task without editing other fields.",
    operation_id="set_task_completion",
)
# PUBLIC_INTERFACE
def set_task_completion(
    id: int = Path(..., ge=1, description="Task id."),
    completed: bool = Body(
        ...,
        embed=True,
        description="Whether the task should be marked as completed.",
        examples=[True],
    ),
) -> TaskCompleteResponse:
    """Mark a task as completed or not completed.

    Args:
        id: Task id.
        completed: Desired completion status.

    Returns:
        TaskCompleteResponse: Wrapper containing the updated task.

    Raises:
        HTTPException: 404 if the task is not found.
    """
    now = _utc_now_iso()
    conn = _get_conn()
    try:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?;", (id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Task not found")

        conn.execute(
            "UPDATE tasks SET completed = ?, updated_at = ? WHERE id = ?;",
            (1 if completed else 0, now, id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (id,)).fetchone()
        assert row is not None
        return TaskCompleteResponse(task=_row_to_task(row))
    finally:
        conn.close()


@app.delete(
    "/tasks/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Tasks"],
    summary="Delete task",
    description="Delete a task by id.",
    operation_id="delete_task",
)
# PUBLIC_INTERFACE
def delete_task(id: int = Path(..., ge=1, description="Task id.")) -> Response:
    """Delete a task.

    Args:
        id: Task id.

    Returns:
        Response: Empty 204 response.

    Raises:
        HTTPException: 404 if the task is not found.
    """
    conn = _get_conn()
    try:
        existing = conn.execute("SELECT id FROM tasks WHERE id = ?;", (id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Task not found")

        conn.execute("DELETE FROM tasks WHERE id = ?;", (id,))
        conn.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    finally:
        conn.close()
