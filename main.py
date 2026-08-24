import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

app = FastAPI()


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


class DraftUpdate(BaseModel):
    subject_name: str | None = None
    relationship: str | None = None
    relationship_label: str | None = None
    born_year: int | None = None
    through_year: int | None = None
    subject_is_living: bool | None = None
    never_forget: str | None = None


@app.get("/health")
def health():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
    return {"status": "ok", "database": row["ok"]}


@app.post("/drafts")
def create_draft():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memoir_draft DEFAULT VALUES RETURNING id, token"
        )
        row = cur.fetchone()
    return {"id": row["id"], "token": row["token"]}


@app.patch("/drafts/{draft_id}")
def update_draft(
    draft_id: str,
    body: DraftUpdate,
    x_draft_token: str = Header(...),
):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    sets = ", ".join(f"{name} = %({name})s" for name in fields)
    params = dict(fields)
    params["draft_id"] = draft_id
    params["token"] = x_draft_token

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE memoir_draft
               SET {sets}, updated_at = now()
             WHERE id = %(draft_id)s
               AND token = %(token)s
               AND claimed_at IS NULL
         RETURNING id, subject_name, relationship, relationship_label,
                   born_year, through_year, subject_is_living, never_forget
            """,
            params,
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="draft not found")

    return row