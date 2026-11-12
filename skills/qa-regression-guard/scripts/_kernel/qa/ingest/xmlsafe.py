"""The hardened XML reader every XML report goes through: no entities, no DTD that is not on the format's own list, no loading of any DTD."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional
from xml.parsers import expat

from qa.ingest import ReportInvalid

# (root element name, system id, public id) -> may this DOCTYPE stand? Never with an internal subset.
DoctypeRule = Callable[[str, Optional[str], Optional[str]], bool]


_UNDEFINED_REFERENCE = re.compile(br"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")


class _Forbidden(Exception):
    pass


def refuse_dtd(data: bytes, allowed_doctype: Optional[DoctypeRule] = None) -> None:
    """Run expat over the document first. It decodes any encoding the parser accepts,
    so a DOCTYPE or ENTITY cannot hide behind UTF-16 or a declared charset.

    A DOCTYPE is refused unless ``allowed_doctype`` names it (the tools write one for their own format, for example
    Cobertura's and JaCoCo's) and it carries no internal subset. Entity declarations are always refused, and neither
    expat nor ElementTree ever fetches the DTD a DOCTYPE points to.
    """
    parser = expat.ParserCreate()

    seen = []

    def doctype(name: str, system: Optional[str], public: Optional[str], has_internal_subset: int) -> None:
        if has_internal_subset or allowed_doctype is None or not allowed_doctype(name, system, public):
            raise _Forbidden()
        seen.append(name)

    def refuse(*_args: object) -> None:
        raise _Forbidden()

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except _Forbidden:
        raise ReportInvalid("doctype_or_entity") from None
    except expat.ExpatError as exc:
        raise ReportInvalid("malformed_xml", str(exc)) from exc
    except (ValueError, LookupError) as exc:  # an encoding expat cannot decode
        raise ReportInvalid("unsupported_encoding", str(exc)) from exc
    if seen:
        # With a DTD named, expat drops a reference to an entity nobody declared without a word. Only the five
        # predefined entities and character references may appear, so check the text itself (ASCII-compatible only).
        if b"\x00" in data:
            raise ReportInvalid("unsupported_encoding", "a document with a DOCTYPE must be ASCII-compatible")
        if _UNDEFINED_REFERENCE.search(data):
            raise ReportInvalid("doctype_or_entity", "undefined entity reference")


def parse_xml(data: bytes, allowed_doctype: Optional[DoctypeRule] = None) -> ET.Element:
    refuse_dtd(data, allowed_doctype)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ReportInvalid("malformed_xml", str(exc)) from exc
    except (ValueError, LookupError) as exc:
        raise ReportInvalid("unsupported_encoding", str(exc)) from exc


def local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
