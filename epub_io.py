"""EPUB reading (via EbookLib) and surgical, formatting-preserving writing (via raw zip patch)."""

from __future__ import annotations

import posixpath
import re
import warnings
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import ebooklib
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub

# We deliberately parse XHTML content documents with an HTML parser (lenient,
# handles real-world epub markup); the resulting "this looks like XML" warning
# is expected and not actionable.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

CONTAINER_PATH = "META-INF/container.xml"
XML_DECL_RE = re.compile(rb"^\s*<\?xml[^>]*\?>\s*")

# Block-level tags we treat as translation units. A tag only becomes a unit if
# it doesn't contain any other tag from this set (i.e. we take the innermost
# block, so we never translate the same text twice via a nested wrapper).
#
# "div" is included because real-world epubs (this one included) routinely use
# a bare <div> per line for poetry, license boilerplate, and internal TOC
# entries instead of <p> -- without it, that content is silently never even
# considered for translation. The leaf check below still correctly skips
# *container* divs that wrap other block tags (<p>, nested <div>, etc.), so
# only divs holding direct text become units.
BLOCK_TAGS = {
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "td", "th", "caption", "figcaption", "dt", "dd", "div",
}

# Never look inside these for translation units.
SKIP_ANCESTORS = {"script", "style", "pre", "code"}


@dataclass
class Unit:
    """One translatable block element, tracked across the whole book."""
    doc_index: int
    tag: object  # bs4.Tag, replaced in place after translation
    html: str    # snapshot of the original outerHTML sent to the model


@dataclass
class Chapter:
    archive_path: str
    soup: BeautifulSoup
    had_xml_decl: bool = False
    is_xml: bool = False  # True for XML-mode-parsed docs (currently just .ncx) --
    # BeautifulSoup's XML serializer emits its own declaration on str(), so serialize()
    # must not also prepend one (unlike the HTML-mode-parsed XHTML chapters, which need it).


@dataclass
class LoadedBook:
    chapters: list = field(default_factory=list)  # List[Chapter], in spine order
    units: list = field(default_factory=list)      # List[Unit], global reading order


# EPUB is reflowable -- there's no real "page" until it's rendered at some font
# size/screen size, so any page count is necessarily an estimate. 250 words/page
# is a commonly used rough equivalent for a typical printed book page.
WORDS_PER_PAGE = 250


def estimate_pages(units: list) -> int:
    total_words = sum(len(unit.tag.get_text().split()) for unit in units)
    return max(1, round(total_words / WORDS_PER_PAGE))


def _get_opf_archive_path(zf: zipfile.ZipFile) -> str:
    container_xml = zf.read(CONTAINER_PATH)
    root = ET.fromstring(container_xml)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find(".//c:rootfile", ns)
    return rootfile.get("full-path")


def _is_leaf_block(tag) -> bool:
    if tag.find(lambda t: t.name in BLOCK_TAGS) is not None:
        return False
    for ancestor in tag.parents:
        if getattr(ancestor, "name", None) in SKIP_ANCESTORS:
            return False
    return True


def load_book(path: str) -> LoadedBook:
    """Parse the epub, returning chapters (with live soup trees) and the
    global, reading-order list of translation units across the whole book.

    EbookLib is used only to resolve spine order and item hrefs. Its
    EpubHtml.get_content() re-renders documents through an internal template
    (dropping arbitrary original <head> content in the process), so the
    actual bytes we parse are read directly from the zip instead."""
    book = epub.read_epub(path, options={"ignore_ncx": True})

    with zipfile.ZipFile(path, "r") as zf:
        opf_path = _get_opf_archive_path(zf)
        opf_dir = posixpath.dirname(opf_path)

        result = LoadedBook()

        for item_id, _linear in book.spine:
            item = book.get_item_with_id(item_id)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue

            archive_path = posixpath.normpath(posixpath.join(opf_dir, item.get_name()))
            raw = zf.read(archive_path)

            had_xml_decl = bool(XML_DECL_RE.match(raw))
            raw = XML_DECL_RE.sub(b"", raw, count=1)

            soup = BeautifulSoup(raw, "lxml")
            doc_index = len(result.chapters)
            result.chapters.append(Chapter(archive_path=archive_path, soup=soup, had_xml_decl=had_xml_decl))

            body = soup.body or soup
            for tag in body.find_all(list(BLOCK_TAGS)):
                if not _is_leaf_block(tag):
                    continue
                if not tag.get_text(strip=True):
                    continue
                result.units.append(Unit(doc_index=doc_index, tag=tag, html=str(tag)))

        # toc.ncx: many readers use THIS, not nav.xhtml/spine content, for their chapter
        # list, so its navLabel/docTitle <text> entries need translating too, even though
        # it's outside the spine and would otherwise never be looked at. NCX is real XML
        # (not HTML) and case-sensitive (docTitle, navLabel, navPoint, playOrder) -- parsed
        # with our lenient HTML parser it gets wrapped in <html><body> and lowercased,
        # which would corrupt it, so this uses BeautifulSoup's XML mode instead.
        for archive_path in zf.namelist():
            if not archive_path.lower().endswith(".ncx"):
                continue
            raw = zf.read(archive_path)
            had_xml_decl = bool(XML_DECL_RE.match(raw))
            raw = XML_DECL_RE.sub(b"", raw, count=1)

            soup = BeautifulSoup(raw, "xml")
            doc_index = len(result.chapters)
            result.chapters.append(
                Chapter(archive_path=archive_path, soup=soup, had_xml_decl=had_xml_decl, is_xml=True)
            )

            for tag in soup.find_all("text"):
                if not tag.get_text(strip=True):
                    continue
                result.units.append(Unit(doc_index=doc_index, tag=tag, html=str(tag)))

    return result


def apply_translations(units: list, translations: list) -> None:
    """Swap translated HTML into each unit's original position in its chapter's soup.

    When the translated fragment is a single tag with the same name as the
    original (the expected/instructed case), this mutates that tag's
    attributes/contents in place rather than swapping in a new tag object --
    so `unit.tag` keeps pointing at the live node in the tree, and a unit can
    be re-translated and re-applied any number of times (used by the GUI's
    per-chunk re-translate). Only when the model changes the tag name or
    returns multiple top-level nodes do we fall back to a one-shot replace,
    which still works for a first application but won't survive re-applying
    to that same unit a second time."""
    if len(units) != len(translations):
        raise ValueError(f"unit/translation count mismatch: {len(units)} vs {len(translations)}")
    for unit, translated_html in zip(units, translations):
        fragment = BeautifulSoup(translated_html, "lxml")
        # BeautifulSoup wraps fragments in html/body; pull out the real content.
        new_nodes = list((fragment.body or fragment).contents)
        if not new_nodes:
            continue
        if len(new_nodes) == 1 and getattr(new_nodes[0], "name", None) == unit.tag.name:
            new_tag = new_nodes[0]
            unit.tag.attrs = dict(new_tag.attrs)
            unit.tag.clear()
            for child in list(new_tag.contents):
                unit.tag.append(child.extract())
        else:
            unit.tag.replace_with(*new_nodes)


def write_translated_epub(source_path: str, chapters: list, output_path: str) -> None:
    """Copy the original epub zip entry-by-entry, substituting only the bytes of
    translated chapter documents. Everything else (images, fonts, css, nav, opf,
    the mimetype entry) is preserved byte-for-byte."""

    def serialize(ch: Chapter) -> bytes:
        if ch.is_xml:
            # BeautifulSoup's XML-mode serializer already emits its own <?xml ...?>
            # declaration on str() -- adding another here would produce two.
            return str(ch.soup).encode("utf-8")
        prefix = b"<?xml version=\"1.0\" encoding=\"utf-8\"?>\n" if ch.had_xml_decl else b""
        return prefix + str(ch.soup).encode("utf-8")

    translated_bytes = {ch.archive_path: serialize(ch) for ch in chapters}

    with zipfile.ZipFile(source_path, "r") as zin, \
         zipfile.ZipFile(output_path, "w") as zout:
        for info in zin.infolist():
            data = translated_bytes.get(info.filename)
            if data is None:
                data = zin.read(info.filename)

            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
            new_info.external_attr = info.external_attr
            zout.writestr(new_info, data, compress_type=new_info.compress_type)
