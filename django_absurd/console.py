"""Command-output decoration: the emoji layer, never logging.

The three commands that decorate their banners (``absurd_worker``, ``absurd_beat``,
``absurd_sync_queues``) probe the destination stream before writing a glyph, so a
console whose encoding cannot represent it gets the identical line without the glyph
instead of a ``UnicodeEncodeError`` out of ``OutputWrapper.write``. Log records never
reach here — they stay plain text, always.
"""

from django.core.management.base import OutputWrapper


def build_glyph_prefix(stream: OutputWrapper, glyph: str) -> str:
    """Return ``glyph`` plus a trailing space, or ``""`` when ``stream`` can't hold it.

    A stream with no ``encoding`` attribute — ``io.StringIO``, most test doubles — can
    hold any character, so the glyph stays; only a stream that reports an encoding
    incapable of the glyph loses it. ``OutputWrapper.__getattr__`` delegates
    ``encoding`` to the wrapped stream, so this reaches the real destination either way.
    """
    encoding = getattr(stream, "encoding", None)
    if encoding is not None:
        try:
            glyph.encode(encoding)
        except UnicodeEncodeError:
            return ""
    return f"{glyph} "
