# The memoir as a file somebody can keep.
#
# Nothing here imports fastapi. It returns bytes, or None when the memoir is
# not the caller's; api/chapters.py turns that into a response or a 404.
#
# ---------------------------------------------------------------------------
# Why ReportLab and not an HTML renderer
# ---------------------------------------------------------------------------
# WeasyPrint would let the PDF and the web reader share one stylesheet, which
# is the right answer on a Linux server and a bad one here: it needs GTK,
# Pango and Cairo installed outside Python, and the maintainer develops on
# Windows, where that is an afternoon and a half. A file that cannot be built
# on the machine it is being written on does not get finished.
#
# ReportLab is pure Python, installs from a wheel everywhere, and Platypus —
# its flowable layout engine — already knows how to keep a heading with the
# paragraph under it and how to number pages. The cost is that this file
# describes the page itself rather than pointing at a stylesheet.
#
# ---------------------------------------------------------------------------
# What is in the file, and what is deliberately not
# ---------------------------------------------------------------------------
# In:  a title page, the contents on page one, the prose, the photographs, and
#      every source as a numbered note under the chapter it belongs to.
# Out: the comment layer.
#
# The comments are out because they are the one part of a memoir that is still
# growing. A PDF is a thing you print and put on a shelf, and printing an open
# conversation freezes half of it — the family would be holding a copy of a
# discussion that has since moved on, with no way to tell.
#
# ---------------------------------------------------------------------------
# The photographs, which used to be out
# ---------------------------------------------------------------------------
# They were filtered out in SQL, and that filter outlived its reason. Once the
# assembly step began *deciding* where each photograph belongs — beside which
# paragraph, and whether the page should stop for it — a PDF that dropped them
# was throwing away the judgement it had just paid for, and telling a family
# their book was ready without their pictures in it.
#
# So: each figure is fetched from the private bucket with this service's own
# credentials, scaled to the text measure, and printed after the paragraph it
# anchors to. A `carousel` group cannot animate on paper; its images print in
# sequence, which is what a carousel is when it stops moving.
#
# A photograph that cannot be fetched or decoded is left out with a log line
# and the book is still produced. A family waiting for their memoir should not
# be handed an error because one object in storage went missing.

import io
import logging
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.platypus.tableofcontents import TableOfContents

from src.domain.memoirs.access import owned_memoir
from src.integrations import supabase_storage
from src.integrations.db import db

logger = logging.getLogger(__name__)


class NothingToExport(Exception):
    """The memoir has no chapters, so there is no book to put in a file."""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

# A5 rather than A4. A memoir is read like a book, and a full sheet of A4 holds
# a line so long the eye loses its place returning to the left margin. This is
# the same reason the web reader fixes its measure.
PAGE = A5
MARGIN = 18 * mm


def _styles() -> dict:
    """The whole typographic system, which is four styles and a note.

    Times over Helvetica because this is a book, and one of the two families
    ReportLab can rely on being present everywhere without shipping a font
    file. A memoir set in the system sans reads like a report.
    """
    base = getSampleStyleSheet()

    return {
        "title": ParagraphStyle(
            "MemoirTitle",
            parent=base["Title"],
            fontName="Times-Roman",
            fontSize=26,
            leading=32,
            spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "MemoirSubtitle",
            parent=base["Normal"],
            fontName="Times-Italic",
            fontSize=12,
            leading=18,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#706a60"),
        ),
        "chapter": ParagraphStyle(
            "MemoirChapter",
            parent=base["Heading1"],
            fontName="Times-Roman",
            fontSize=17,
            leading=22,
            spaceBefore=0,
            spaceAfter=14,
        ),
        "body": ParagraphStyle(
            "MemoirBody",
            parent=base["BodyText"],
            fontName="Times-Roman",
            fontSize=10.5,
            leading=16,
            spaceAfter=9,
            firstLineIndent=0,
        ),
        "pull": ParagraphStyle(
            "MemoirPull",
            parent=base["BodyText"],
            fontName="Times-Italic",
            fontSize=11.5,
            leading=17,
            leftIndent=10 * mm,
            rightIndent=10 * mm,
            spaceBefore=6,
            spaceAfter=12,
        ),
        "note": ParagraphStyle(
            "MemoirNote",
            parent=base["BodyText"],
            fontName="Times-Roman",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#706a60"),
            spaceAfter=2,
        ),
        "toc": ParagraphStyle(
            "MemoirToc",
            parent=base["BodyText"],
            fontName="Times-Roman",
            fontSize=11,
            leading=18,
        ),
    }


def _page_number(canvas, doc):
    """A folio, centred, from the first chapter page onwards.

    The title page and the contents do not carry one, which is the convention
    every printed book follows and the reason the document uses two page
    templates rather than one.
    """
    canvas.saveState()
    canvas.setFont("Times-Roman", 8)
    canvas.setFillColor(colors.HexColor("#a69e91"))
    canvas.drawCentredString(PAGE[0] / 2, MARGIN * 0.6, str(canvas.getPageNumber()))
    canvas.restoreState()


def _blank(canvas, doc):
    return None


# ---------------------------------------------------------------------------
# Reading the book out of the database
# ---------------------------------------------------------------------------


async def _chapters(cur, memoir_id: str) -> list[dict]:
    await cur.execute(
        """
        SELECT id, ordinal, title, from_year, through_year
          FROM chapter
         WHERE memoir_id = %(memoir)s
         ORDER BY ordinal
        """,
        {"memoir": memoir_id},
    )
    return await cur.fetchall()


async def _blocks(cur, memoir_id: str) -> dict:
    """`{chapter_id: [block, ...]}` — prose only, in reading order.

    Figures are still excluded here, but they are no longer excluded from the
    book: `_figures` reads them separately, keyed by the paragraph they anchor
    to, because that is how they are laid out. A figure has no position in the
    prose sequence — it has a paragraph it belongs beside.
    """
    await cur.execute(
        """
        SELECT id, chapter_id, ordinal, kind::text AS kind, text
          FROM chapter_block
         WHERE memoir_id = %(memoir)s
           AND kind <> 'figure'
         ORDER BY chapter_id, ordinal
        """,
        {"memoir": memoir_id},
    )
    held: dict = {}
    for row in await cur.fetchall():
        held.setdefault(str(row["chapter_id"]), []).append(row)
    return held


async def _figures(cur, memoir_id: str) -> dict:
    """`{anchor_block_id: [figure, ...]}` — the photographs and their captions.

    Keyed by anchor rather than by chapter, so `_story` can print a figure
    directly after the paragraph it was placed against and a carousel group
    comes out in one run: figures sharing an anchor arrive together, in
    `ordinal` order, which is what makes the grouping implicit rather than
    stored.

    The caption is the contributor's own words about the photograph, read from
    the memory it came with — the same rule the web reader follows, and the
    reason nothing here generates one. `storage_path` is selected because the
    bytes have to be fetched; it goes no further than this module.
    """
    await cur.execute(
        """
        SELECT b.id,
               b.anchor_block_id,
               b.ordinal,
               b.placement::text AS placement,
               a.storage_path,
               a.mime_type,
               (SELECT m.body_text
                  FROM memory m
                 WHERE m.id = a.memory_id) AS caption
          FROM chapter_block b
          JOIN media_asset a ON a.id = b.asset_id
         WHERE b.memoir_id = %(memoir)s
           AND b.kind = 'figure'
         ORDER BY b.anchor_block_id, b.ordinal
        """,
        {"memoir": memoir_id},
    )
    held: dict = {}
    for row in await cur.fetchall():
        held.setdefault(str(row["anchor_block_id"]), []).append(row)
    return held


async def _sources(cur, memoir_id: str) -> dict:
    """`{block_id: [credit, ...]}` — who each paragraph came from.

    The same join `chapter_service._sources` makes for the web reader, minus
    the character offsets: a note under a chapter says who a paragraph came
    from, and a printed page has no way to light up the clause it covers.
    """
    await cur.execute(
        """
        SELECT s.block_id,
               p.display_name AS name,
               p.relationship_label AS label,
               mem.kind::text AS medium,
               COALESCE(
                   EXTRACT(YEAR FROM mem.happened_on)::int,
                   EXTRACT(YEAR FROM mem.created_at)::int
               ) AS year,
               s.diverges
          FROM block_source s
          JOIN memoir_participant p
            ON p.memoir_id = s.memoir_id AND p.id = s.participant_id
          JOIN memory mem
            ON mem.memoir_id = s.memoir_id AND mem.id = s.memory_id
         WHERE s.memoir_id = %(memoir)s
         ORDER BY s.block_id, s.start_offset NULLS FIRST
        """,
        {"memoir": memoir_id},
    )
    held: dict = {}
    for row in await cur.fetchall():
        held.setdefault(str(row["block_id"]), []).append(row)
    return held


MEDIUM = {"voice": "recorded", "photo": "photograph", "text": "written"}


def _credit(source: dict) -> str:
    """One note: who, how, when, and whether they disagreed.

    "Margaret Reyes, recorded 1994" — and "differs" on an account that
    contradicts the prose above it. Both accounts are kept and neither is
    corrected, so the note has to say which is which or the page would be
    quietly choosing.
    """
    who = source["name"]
    if source["label"]:
        who = f"{who}, {source['label']}"

    how = MEDIUM.get(source["medium"], source["medium"])
    when = f" {source['year']}" if source["year"] else ""
    differs = " — differs from the account above" if source["diverges"] else ""

    return f"{who} · {how}{when}{differs}"


# The widest a photograph may print: the text measure, so a figure lines up
# with the prose above it rather than bleeding into the margin. Height is
# capped separately at a little over half the page, because a portrait
# photograph scaled to the full measure would otherwise push the paragraph it
# belongs to onto the page before it.
_FIGURE_WIDTH = PAGE[0] - 2 * MARGIN
_FIGURE_MAX_HEIGHT = (PAGE[1] - 2 * MARGIN) * 0.55

# What we will pull out of storage for one photograph. The same ceiling the
# planner uses, for the same reason: one enormous object should not decide how
# long an export takes.
_MAX_ASSET_BYTES = 25 * 1024 * 1024


async def _load_figures(figures: dict) -> dict:
    """`{block_id: bytes}` for every figure whose object could be read.

    Fetched here, in one pass, rather than inside `_story` — which is
    synchronous, and would otherwise need an await in the middle of building a
    flowable list. It is also the only place in this file that touches the
    network, which keeps `_story` a pure function of what it is given.

    A photograph that cannot be fetched is simply absent from the result, and
    `_story` prints the ones it has. The book is worth more than the guarantee
    that it is complete.
    """
    loaded: dict = {}

    for run in figures.values():
        for figure in run:
            try:
                loaded[str(figure["id"])] = await supabase_storage.download_object(
                    figure["storage_path"], max_bytes=_MAX_ASSET_BYTES
                )
            except supabase_storage.StorageError as exc:
                logger.warning("Leaving a photograph out of the PDF: %s", exc)

    return loaded


def _plate(figure: dict, raw: bytes, style: dict) -> list:
    """One photograph as flowables: the image, and its caption if it has one.

    Scaled by aspect ratio rather than stretched — `Image(width=, height=)` in
    ReportLab does not preserve it, so both are computed here from the real
    pixel size. `KeepTogether` is what stops a caption being orphaned onto the
    next page away from the picture it describes.

    Returns an empty list for an image ReportLab cannot open, which is the same
    outcome as one that could not be fetched: this figure is not in the book,
    and the rest of it is.
    """
    try:
        reader = ImageReader(io.BytesIO(raw))
        pixel_width, pixel_height = reader.getSize()
    except (OSError, ValueError) as exc:
        # A truncated file, or one that is not an image at all. Pillow's
        # `UnidentifiedImageError` is an `OSError`, so both are covered, and
        # naming them rather than catching everything keeps a real bug in this
        # function visible instead of silently costing a photograph.
        logger.warning("Could not decode a photograph for the PDF: %s", exc)
        return []

    if not pixel_width or not pixel_height:
        return []

    width = _FIGURE_WIDTH
    height = width * pixel_height / pixel_width
    if height > _FIGURE_MAX_HEIGHT:
        height = _FIGURE_MAX_HEIGHT
        width = height * pixel_width / pixel_height

    plate: list = [
        Spacer(1, 4 * mm),
        Image(io.BytesIO(raw), width=width, height=height),
    ]

    caption = (figure["caption"] or "").strip()
    if caption:
        # The contributor's own words. Nothing here writes a caption — see the
        # fabrication rule, and `planner._SYSTEM`, which forbids the model
        # describing a photograph even though it can see one.
        plate.append(Spacer(1, 2 * mm))
        plate.append(Paragraph(_escape(caption), style["note"]))

    plate.append(Spacer(1, 4 * mm))
    return [KeepTogether(plate)]


def _escape(text: str) -> str:
    """ReportLab reads a subset of HTML in a Paragraph, so a memory containing
    "<" would either vanish or raise. Family text is exactly where an ampersand
    turns up."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Building it
# ---------------------------------------------------------------------------


class MemoirDocument(BaseDocTemplate):
    """A document that tells its own contents page where things landed.

    `afterFlowable` is Platypus's hook for exactly this: as each flowable is
    laid out, the template gets a look at it and can `notify` the
    TableOfContents of an entry and the page it fell on. It is why the file is
    built twice — the first pass collects the numbers, the second prints them.

    Marking the headings with an attribute rather than checking their style
    keeps the decision where the heading is created, so a future flowable that
    should appear in the contents says so itself.
    """

    def afterFlowable(self, flowable):
        entry = getattr(flowable, "toc_title", None)
        if entry is not None:
            self.notify("TOCEntry", (0, entry, self.page))


def _document(buffer: io.BytesIO, title: str, author: str) -> BaseDocTemplate:
    doc = MemoirDocument(
        buffer,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=title,
        author=author,
        subject="A memoir",
    )
    frame = Frame(
        MARGIN, MARGIN, PAGE[0] - 2 * MARGIN, PAGE[1] - 2 * MARGIN, id="page"
    )
    doc.addPageTemplates(
        [
            PageTemplate(id="front", frames=[frame], onPage=_blank),
            PageTemplate(id="body", frames=[frame], onPage=_page_number),
        ]
    )
    return doc


def _story(
    memoir: dict,
    chapters: list[dict],
    blocks: dict,
    sources: dict,
    figures: dict | None = None,
    images: dict | None = None,
) -> list:
    """Everything on the page, in order, as flowables.

    `figures` is `{anchor_block_id: [figure, ...]}` and `images` is
    `{figure_block_id: bytes}`. Both default to empty so a caller that only
    wants the prose — and every existing test — still works unchanged.
    """
    style = _styles()
    story: list = []

    # --- the title page ---------------------------------------------------
    years = ""
    if memoir["born_year"]:
        ends = memoir["through_year"] or ("Present" if memoir["subject_is_living"] else "")
        years = f"{memoir['born_year']} — {ends}" if ends else str(memoir["born_year"])

    story.append(Spacer(1, PAGE[1] * 0.22))
    story.append(Paragraph(_escape(memoir["subject_name"]), style["title"]))
    if years:
        story.append(Paragraph(years, style["subtitle"]))
    story.append(Spacer(1, 10 * mm))
    story.append(
        Paragraph("As remembered by everyone who knew them", style["subtitle"])
    )

    # --- the contents -----------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Contents", style["chapter"]))

    toc = TableOfContents()
    toc.levelStyles = [style["toc"]]
    # Dots to the folio, which is what makes a contents page readable across
    # the width rather than two columns of text with a gap between them.
    toc.dotsMinLevel = 0
    story.append(toc)

    # --- the chapters -----------------------------------------------------
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    for index, chapter in enumerate(chapters):
        if index:
            story.append(PageBreak())

        heading = Paragraph(_escape(chapter["title"]), style["chapter"])
        # What puts the chapter in the contents. `notify` is Platypus's own
        # channel from a flowable back to the document, which is why this file
        # builds twice — the first pass discovers the page numbers, the second
        # prints them.
        heading.toc_title = chapter["title"]
        story.append(heading)

        if chapter["from_year"]:
            span = str(chapter["from_year"])
            if chapter["through_year"] and chapter["through_year"] != chapter["from_year"]:
                span = f"{chapter['from_year']} — {chapter['through_year']}"
            story.append(Paragraph(span, style["note"]))
            story.append(Spacer(1, 4 * mm))

        notes: list[str] = []

        for block in blocks.get(str(chapter["id"]), []):
            text = _escape(block["text"] or "")
            credits = sources.get(str(block["id"]), [])

            if credits:
                # One marker per paragraph, numbered within the chapter. Where
                # a paragraph was assembled from several people it carries
                # several numbers, which is the printed form of the reader's
                # reciprocal highlighting.
                marks = []
                for credit in credits:
                    notes.append(_credit(credit))
                    marks.append(str(len(notes)))
                text += f'<super rise="3" size="6">{", ".join(marks)}</super>'

            story.append(
                Paragraph(text, style["pull" if block["kind"] == "pull" else "body"])
            )

            # The photographs placed against this paragraph, in order. A
            # carousel group is several figures sharing this anchor, and on
            # paper that is simply a run of plates — there is nothing to
            # advance.
            for figure in (figures or {}).get(str(block["id"]), []):
                raw = (images or {}).get(str(figure["id"]))
                if raw is None:
                    continue
                story.extend(_plate(figure, raw, style))

        if notes:
            story.append(Spacer(1, 6 * mm))
            story.append(
                Paragraph(
                    '<font color="#a69e91">' + "—" * 12 + "</font>", style["note"]
                )
            )
            for number, note in enumerate(notes, start=1):
                story.append(Paragraph(f"{number}. {_escape(note)}", style["note"]))

    # --- the colophon -----------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("How this book was made", style["chapter"]))
    story.append(
        Paragraph(
            "Assembled from memories left by the people who knew "
            f"{_escape(memoir['subject_name'])}. Every paragraph carries the "
            "sources it was drawn from, and where two people remembered the "
            "same afternoon differently, both accounts were kept and neither "
            "was corrected.",
            style["body"],
        )
    )
    story.append(Spacer(1, 4 * mm))
    # `%-d` strips the leading zero on Linux and raises on Windows, which is
    # where this is being written. lstrip does it everywhere.
    story.append(
        Paragraph(
            f'Exported {date.today().strftime("%d %B %Y").lstrip("0")}.',
            style["note"],
        )
    )

    return story


async def export_pdf(memoir_id: str, user_id: str) -> tuple[bytes, str] | None:
    """The memoir as a PDF. Returns `(bytes, filename)`, or None if not theirs.

    Raises NothingToExport when there are no chapters yet.

    Generated on the fly rather than cached. A published memoir never changes,
    so a cache would be safe — and it would also be a second copy of a family's
    memoir sitting in object storage, invalidation to get wrong, and a bucket
    to secure. A5 prose is a few hundred kilobytes and a second of CPU; when
    that stops being true, the place to put the cache is in front of this
    function rather than inside it.
    """
    async with db() as conn, conn.cursor() as cur:
        owned = await owned_memoir(cur, memoir_id, user_id)
        if owned is None:
            return None

        await cur.execute(
            """
            SELECT subject_name, born_year, through_year, subject_is_living
              FROM memoir
             WHERE id = %(id)s
            """,
            {"id": memoir_id},
        )
        memoir = await cur.fetchone()

        chapters = await _chapters(cur, memoir_id)
        if not chapters:
            raise NothingToExport

        blocks = await _blocks(cur, memoir_id)
        sources = await _sources(cur, memoir_id)
        figures = await _figures(cur, memoir_id)

    # Outside the transaction, like every other call this codebase makes to
    # another service: holding a Postgres connection open across a fetch of a
    # dozen photographs is how a bounded pool dies.
    images = await _load_figures(figures)

    buffer = io.BytesIO()
    doc = _document(buffer, memoir["subject_name"], "The Memoir Project")
    # Twice, on purpose: the first pass records where each chapter landed and
    # the second prints those numbers into the contents.
    doc.multiBuild(_story(memoir, chapters, blocks, sources, figures, images))

    filename = "".join(
        c if c.isalnum() or c in " -_" else "" for c in memoir["subject_name"]
    ).strip()
    logger.info("Exported memoir %s as a PDF", memoir_id)

    return buffer.getvalue(), f"{filename or 'memoir'}.pdf"
