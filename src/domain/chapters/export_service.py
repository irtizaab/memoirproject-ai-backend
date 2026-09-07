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
# In:  a title page, the contents on page one, the prose, and every source as
#      a numbered note under the chapter it belongs to.
# Out: the comment layer, and the photographs.
#
# The comments are out because they are the one part of a memoir that is still
# growing. A PDF is a thing you print and put on a shelf, and printing an open
# conversation freezes half of it — the family would be holding a copy of a
# discussion that has since moved on, with no way to tell.
#
# The photographs are out of this pass because a private bucket, signed URLs
# and image scaling are their own slice, and a family who want the book in
# their hands should not wait for it. The sources say what each photograph was
# — they are named in the notes like everything else.

import io
import logging
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.platypus.tableofcontents import TableOfContents

from src.domain.memoirs.access import owned_memoir
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
    """`{chapter_id: [block, ...]}`, prose only, in reading order.

    Figures are filtered out in SQL rather than skipped later, so the ordinals
    that survive are the ones that will be printed and nothing has to remember
    that a gap is expected.
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


def _story(memoir: dict, chapters: list[dict], blocks: dict, sources: dict) -> list:
    """Everything on the page, in order, as flowables."""
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

    buffer = io.BytesIO()
    doc = _document(buffer, memoir["subject_name"], "The Memoir Project")
    # Twice, on purpose: the first pass records where each chapter landed and
    # the second prints those numbers into the contents.
    doc.multiBuild(_story(memoir, chapters, blocks, sources))

    filename = "".join(
        c if c.isalnum() or c in " -_" else "" for c in memoir["subject_name"]
    ).strip()
    logger.info("Exported memoir %s as a PDF", memoir_id)

    return buffer.getvalue(), f"{filename or 'memoir'}.pdf"
