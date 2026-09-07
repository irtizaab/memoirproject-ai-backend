# Finding one afternoon in a life.
#
# Nothing here imports fastapi. It returns plain dicts and None; the API layer
# decides what a None means.
#
# ---------------------------------------------------------------------------
# One corpus, two doors
# ---------------------------------------------------------------------------
# The owner searching from the archive and a family member searching from the
# reader get the same results out of the same query. That is a product
# decision, not an implementation shortcut: a memoir is a shared object, and a
# search that quietly returned less to the family than to the owner would make
# them wonder what else was being kept from them.
#
# The two callers differ only in how they prove they may look at all, which is
# `owned_memoir` on one side and the reader session on the other, both resolved
# before anything here runs.
#
# ---------------------------------------------------------------------------
# The four kinds of thing a search can find
# ---------------------------------------------------------------------------
#   chapter     a paragraph of the book, with the chapter it is in
#   photo       a photograph, found through the caption its giver wrote
#   recording   something said out loud, found through its transcript
#   reflection  something a reader wrote in the margins
#
# They are one UNION rather than four endpoints because "where is that story"
# is one question, and a person asking it does not know which of the four they
# are looking for. The counts per kind fall out of the same query, so the
# filters on the results screen never disagree with the results.

import logging

from src.integrations.db import db

logger = logging.getLogger(__name__)

# The dictionary every expression index in migration 0013 was built with.
# Changing it here without changing them there does not break correctness — it
# silently stops the indexes being used, which is worse, because it looks fine.
DICTIONARY = "english"

# Delimiters `ts_headline` wraps a match in.
#
# Not `<mark>`: the excerpt is built from text a reader typed, and returning
# HTML would mean the frontend rendering user input as markup. These are two
# control characters that cannot occur in text somebody typed on a phone, so
# the frontend can split on them and emit its own element.
HIGHLIGHT_START = "\x02"
HIGHLIGHT_END = "\x03"

# Passed to `ts_headline` as a bound parameter rather than written into the
# query text. Two control characters sitting in the middle of a SQL literal
# survive a copy-paste and an editor's whitespace pass right up until they do
# not — and a value belongs in a placeholder regardless.
_HEADLINE = (
    f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_END}, "
    "MaxWords=40, MinWords=18, ShortWord=3, MaxFragments=1"
)

# A cap rather than pagination. Sixty hits is more than anybody reads, and a
# family archive does not have a thousandth page — when one does, the cursor
# goes here and the frontend gets a "more" affordance it currently has no use
# for. Written into the query below rather than interpolated: no value, not
# even a constant of ours, is ever glued into SQL text in this codebase.
LIMIT = 60


_SEARCH_SQL = """
WITH q AS (
    SELECT websearch_to_tsquery('english', %(query)s) AS tsq
),

-- The prose. `chapter_id` and `block_id` are what let a result be a link
-- straight to the paragraph rather than to the top of a chapter.
prose AS (
    SELECT 'chapter'                                     AS kind,
           b.id                                          AS id,
           c.id                                          AS chapter_id,
           c.title                                       AS title,
           c.from_year                                   AS year,
           ts_headline('english', b.text, q.tsq, %(headline)s) AS excerpt,
           NULL::text                                    AS attribution,
           ts_rank(to_tsvector('english', coalesce(b.text, '')), q.tsq) AS rank
      FROM chapter_block b
      JOIN chapter c ON c.id = b.chapter_id
      CROSS JOIN q
     WHERE b.memoir_id = %(memoir_id)s
       AND b.kind <> 'figure'
       AND to_tsvector('english', coalesce(b.text, '')) @@ q.tsq
),

-- Photographs, reached through the caption the person who gave it wrote. A
-- memory can hold several images; the result is the memory, because that is
-- what the caption describes.
photos AS (
    SELECT 'photo'                                       AS kind,
           mem.id                                        AS id,
           NULL::uuid                                    AS chapter_id,
           coalesce(nullif(btrim(mem.title), ''), 'Photograph') AS title,
           EXTRACT(YEAR FROM coalesce(mem.happened_on, mem.created_at))::int AS year,
           ts_headline('english',
                       coalesce(mem.body_text, mem.title, ''),
                       q.tsq, %(headline)s)     AS excerpt,
           p.display_name                                AS attribution,
           ts_rank(to_tsvector('english',
                   coalesce(mem.title, '') || ' ' || coalesce(mem.body_text, '')),
                   q.tsq)                                AS rank
      FROM memory mem
      JOIN memoir_participant p
        ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
      CROSS JOIN q
     WHERE mem.memoir_id = %(memoir_id)s
       AND EXISTS (SELECT 1 FROM media_asset a
                    WHERE a.memory_id = mem.id
                      AND a.kind = 'image'
                      AND a.uploaded_at IS NOT NULL)
       AND to_tsvector('english',
               coalesce(mem.title, '') || ' ' || coalesce(mem.body_text, '')) @@ q.tsq
),

-- What was said out loud, found in the transcript rather than the audio.
recordings AS (
    SELECT 'recording'                                   AS kind,
           mem.id                                        AS id,
           NULL::uuid                                    AS chapter_id,
           coalesce(nullif(btrim(mem.title), ''), 'Recording') AS title,
           EXTRACT(YEAR FROM coalesce(mem.happened_on, mem.created_at))::int AS year,
           ts_headline('english', t.text, q.tsq, %(headline)s) AS excerpt,
           p.display_name                                AS attribution,
           ts_rank(to_tsvector('english', coalesce(t.text, '')), q.tsq) AS rank
      FROM transcript t
      JOIN media_asset a ON a.id = t.asset_id
      JOIN memory mem ON mem.id = a.memory_id
      JOIN memoir_participant p
        ON p.memoir_id = mem.memoir_id AND p.id = mem.participant_id
      CROSS JOIN q
     WHERE mem.memoir_id = %(memoir_id)s
       AND t.status = 'done'
       AND to_tsvector('english', coalesce(t.text, '')) @@ q.tsq
),

-- The layer that keeps growing, and otherwise the least findable material in
-- the product: the thing an uncle wrote in the margins three years ago.
reflections AS (
    SELECT 'reflection'                                  AS kind,
           cm.id                                         AS id,
           th.chapter_id                                 AS chapter_id,
           c.title                                       AS title,
           EXTRACT(YEAR FROM cm.created_at)::int         AS year,
           ts_headline('english', cm.body, q.tsq, %(headline)s) AS excerpt,
           p.display_name                                AS attribution,
           ts_rank(to_tsvector('english', cm.body), q.tsq) AS rank
      FROM comment cm
      JOIN comment_thread th ON th.id = cm.thread_id
      JOIN chapter c ON c.id = th.chapter_id
      JOIN memoir_participant p
        ON p.memoir_id = cm.memoir_id AND p.id = cm.participant_id
      CROSS JOIN q
     WHERE cm.memoir_id = %(memoir_id)s
       AND to_tsvector('english', cm.body) @@ q.tsq
)

SELECT * FROM prose
UNION ALL SELECT * FROM photos
UNION ALL SELECT * FROM recordings
UNION ALL SELECT * FROM reflections
ORDER BY rank DESC, year NULLS LAST
LIMIT 60
"""


async def search_memoir(memoir_id: str, query: str) -> dict:
    """Everything in one memoir that matches, and how many of each kind.

    Returns `{query, total, counts, hits}`. An empty or unparseable query
    returns an empty result rather than raising: somebody who has typed one
    character and paused has not made a mistake.

    `websearch_to_tsquery` rather than `plainto_tsquery`, so "lake george"
    behaves the way a person expects from every other search box they have
    used — quoted phrases hold together and `-word` excludes.

    Counting is done in Python over the rows the query returned rather than as
    a second aggregate query. It is the same rows, so the filter counts and the
    results can never disagree — and they would, briefly and confusingly, if
    somebody left a comment between the two queries.
    """
    query = (query or "").strip()
    if not query:
        return {"query": query, "total": 0, "counts": {}, "hits": []}

    async with db() as conn, conn.cursor() as cur:
        await cur.execute(
            _SEARCH_SQL,
            {"memoir_id": memoir_id, "query": query, "headline": _HEADLINE},
        )
        hits = await cur.fetchall()

    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit["kind"]] = counts.get(hit["kind"], 0) + 1

    logger.info("Search in memoir %s returned %s hits", memoir_id, len(hits))

    return {
        "query": query,
        "total": len(hits),
        "counts": counts,
        "hits": hits,
    }
