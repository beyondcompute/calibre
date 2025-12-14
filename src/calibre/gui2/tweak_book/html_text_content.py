#!/usr/bin/env python
# License: GPLv3

import re
import unittest

from calibre.utils.icu import utf16_length


_ATTR_RE_TEMPLATE = r'''(?ix)
    (?:^|[\s/])
    ({attrs})
    \s*=\s*
    (["'])
    (.*?)
    \2
'''


def _find_tag_end(raw: str, start: int) -> int:
    quote = None
    i = start
    while i < len(raw):
        c = raw[i]
        if quote is not None:
            if c == quote:
                quote = None
        else:
            if c in ('"', "'"):
                quote = c
            elif c == '>':
                return i
        i += 1
    return -1


def iter_text_and_attr_spans(
    raw: str,
    *,
    include_attributes: tuple[str, ...] = ('title', 'alt'),
    skip_element_text: tuple[str, ...] = ('script', 'style'),
):
    '''
    Yield (start, end) spans inside raw that correspond to:
      - text content outside tags
      - attribute values for attributes in include_attributes (values only, without quotes)

    Spans never cross tag boundaries.
    '''
    if not raw:
        return

    attr_pat = None
    if include_attributes:
        attrs = '|'.join(map(re.escape, include_attributes))
        attr_pat = re.compile(_ATTR_RE_TEMPLATE.format(attrs=attrs))

    skip_stack = []

    i = 0
    n = len(raw)
    while i < n:
        if raw.startswith('<!--', i):
            j = raw.find('-->', i + 4)
            if j < 0:
                return
            i = j + 3
            continue

        if raw[i] != '<':
            j = raw.find('<', i)
            if j < 0:
                j = n
            if not skip_stack and j > i:
                yield i, j
            i = j
            continue

        # We are at a tag-ish thing. Find its end, respecting quotes.
        j = _find_tag_end(raw, i + 1)
        if j < 0:
            return

        tag = raw[i + 1 : j]
        tag_stripped = tag.lstrip()
        is_end_tag = tag_stripped.startswith('/')

        # Extract tag name
        m = re.match(r'/?\s*([A-Za-z][A-Za-z0-9:_-]*)', tag_stripped)
        tag_name = (m.group(1).lower() if m else '')
        is_self_closing = tag.rstrip().endswith('/')

        # Attribute spans (only on start tags)
        if (not is_end_tag) and attr_pat is not None and tag_name:
            # Match on the tag contents only, then translate to raw offsets.
            for am in attr_pat.finditer(tag):
                # value spans are group(3) in tag-local coordinates
                val_start = i + 1 + am.start(3)
                val_end = i + 1 + am.end(3)
                if val_end > val_start:
                    yield val_start, val_end

        # Track whether we are inside script/style.
        if tag_name in skip_element_text and tag_name:
            if is_end_tag:
                if skip_stack and skip_stack[-1] == tag_name:
                    skip_stack.pop()
            elif not is_self_closing:
                skip_stack.append(tag_name)

        i = j + 1


def replace_in_text_and_attributes(
    raw: str,
    pat,
    repl,
    *,
    include_attributes: tuple[str, ...] = ('title', 'alt'),
    skip_element_text: tuple[str, ...] = ('script', 'style'),
    replace: bool = True,
) -> tuple[str, int]:
    spans = tuple(iter_text_and_attr_spans(
        raw, include_attributes=include_attributes, skip_element_text=skip_element_text
    ))
    if not spans:
        return raw, 0

    # Apply replacements from end to start to preserve offsets.
    out = raw
    total = 0
    for start, end in reversed(spans):
        segment = out[start:end]
        if replace:
            new, num = pat.subn(repl, segment)
        else:
            num = len(pat.findall(segment))
            new = segment
        if num:
            total += num
            if replace:
                out = out[:start] + new + out[end:]
    return out, total


def python_index_from_utf16_offset(text: str, utf16_offset: int) -> int:
    if utf16_offset <= 0:
        return 0
    seen = 0
    for i, ch in enumerate(text):
        seen += 2 if ord(ch) > 0xFFFF else 1
        if seen > utf16_offset:
            return i
    return len(text)


def find_first_match_span(raw: str, pat, *, start_at_utf16: int = 0) -> tuple[int, int] | None:
    start_at = python_index_from_utf16_offset(raw, start_at_utf16)
    for start, end in iter_text_and_attr_spans(raw):
        if end <= start_at:
            continue
        local_start = 0
        if start < start_at < end:
            local_start = start_at - start
        m = pat.search(raw[start + local_start : end])
        if m is None:
            continue
        ms, me = m.span()
        return start + local_start + ms, start + local_start + me
    return None


def find_last_match_span(raw: str, pat, *, end_at_utf16: int | None = None) -> tuple[int, int] | None:
    end_at = len(raw) if end_at_utf16 is None else python_index_from_utf16_offset(raw, end_at_utf16)
    spans = tuple(iter_text_and_attr_spans(raw))
    for start, end in reversed(spans):
        if start >= end_at:
            continue
        local_end = end - start
        if start < end_at < end:
            local_end = end_at - start
        m = pat.search(raw[start : start + local_end])
        if m is None:
            continue
        ms, me = m.span()
        return start + ms, start + me
    return None


def span_is_replaceable(raw: str, start: int, end: int, *, include_attributes: tuple[str, ...] = ('title', 'alt')) -> bool:
    if start < 0 or end < start or end > len(raw):
        return False
    for s, e in iter_text_and_attr_spans(raw, include_attributes=include_attributes):
        if s <= start and end <= e:
            return True
    return False


class TestHTMLTextContent(unittest.TestCase):

    def test_replaces_text_not_tags(self):
        import regex

        raw = '<p>Hello <b>world</b></p>'
        pat = regex.compile('world')
        out, num = replace_in_text_and_attributes(raw, pat, 'there')
        self.assertEqual(num, 1)
        self.assertEqual(out, '<p>Hello <b>there</b></p>')

    def test_does_not_cross_tags(self):
        import regex

        raw = '<p>Hel<b>lo</b></p>'
        pat = regex.compile('Hello')
        out, num = replace_in_text_and_attributes(raw, pat, 'X')
        self.assertEqual(num, 0)
        self.assertEqual(out, raw)

    def test_replaces_title_and_alt(self):
        import regex

        raw = '<p title="Hello">x<img alt=\'Hello\'/></p>'
        pat = regex.compile('Hello')
        out, num = replace_in_text_and_attributes(raw, pat, 'Yo')
        self.assertEqual(num, 2)
        self.assertIn('title="Yo"', out)
        self.assertIn("alt='Yo'", out)

    def test_skips_script_text(self):
        import regex

        raw = '<p>Hello</p><script>var Hello = 1</script>'
        pat = regex.compile('Hello')
        out, num = replace_in_text_and_attributes(raw, pat, 'Yo')
        self.assertEqual(num, 1)
        self.assertEqual(out, '<p>Yo</p><script>var Hello = 1</script>')

    def test_find_first_match_span_utf16(self):
        import regex

        raw = '<p>😀 Hello 😀</p>'
        # cursor after the first emoji and space in raw source string, not rendered
        start_at_utf16 = utf16_length('<p>😀 ')
        pat = regex.compile('Hello')
        span = find_first_match_span(raw, pat, start_at_utf16=start_at_utf16)
        self.assertEqual(raw[span[0]:span[1]], 'Hello')


def find_tests():
    return unittest.defaultTestLoader.loadTestsFromTestCase(TestHTMLTextContent)
