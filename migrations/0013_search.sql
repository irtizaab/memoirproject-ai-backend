-- ============================================================================
-- The Memoir Project — migration 0013
-- Finding one afternoon in a life.
--
-- A memoir is the one kind of document where "I know it is in here somewhere"
-- is the normal way to arrive: somebody remembers a dock, or a blue enamel
-- percolator, and wants the page it is on. Scrolling eight chapters to find it
-- is the failure this closes.
--
-- Four expression indexes and no new columns. Postgres can use an index on
-- `to_tsvector(...)` when a query names the identical expression, which means
-- no tsvector columns to keep in step and no triggers to forget. A materialized
-- column would be faster to sort on and would need one of those triggers per
-- table; at family scale — hundreds of rows, not millions — the expression is
-- the right trade and can be promoted later without changing a query.
--
-- What is searched, and what is not:
--
--   in   chapter prose, photograph captions, voice transcripts, and the
--        reflections the family have left
--   out  `memoir.never_forget`
--
-- That exclusion is the same one `LinkInvitation` and `MemoirReading` make.
-- It is the owner's private answer to what should never be forgotten about the
-- subject, it is filtered out of every response in the API, and an index over
-- it would be the one place it could come back — as a search result, to
-- whoever holds the link.
-- ============================================================================

BEGIN;


-- The prose. `kind <> 'figure'` because a figure block holds no text at all;
-- indexing it would be an index of empty strings.
CREATE INDEX chapter_block_search_idx ON chapter_block
    USING gin (to_tsvector('english', coalesce(text, '')))
    WHERE kind <> 'figure';


-- Memories, which is how both photographs and typed recollections are reached:
-- a photograph's caption is its memory's `body_text`, written by the person who
-- gave it. `_figures()` already reads captions from there rather than
-- generating them, and this agrees with that.
CREATE INDEX memory_search_idx ON memory
    USING gin (to_tsvector('english',
        coalesce(title, '') || ' ' || coalesce(body_text, '')));


-- What was said out loud. Only finished transcripts carry text, and a partial
-- index keeps the queued and failed ones out of it.
CREATE INDEX transcript_search_idx ON transcript
    USING gin (to_tsvector('english', coalesce(text, '')))
    WHERE status = 'done';


-- The layer that keeps growing. Searching it is the reason a family can find
-- the thing an uncle said in a comment three years ago, which is otherwise the
-- least findable material in the product.
CREATE INDEX comment_search_idx ON comment
    USING gin (to_tsvector('english', body));


COMMIT;
