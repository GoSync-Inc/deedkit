"""ANYM-1000: a live search hit's text cut to a width at a word boundary, marked when it was cut."""
ELLIPSIS = '…'


def snippet(text, width):
    """{'text': ...} within width characters; a cut text ends in an ellipsis and adds text_truncated: true.

    The cut falls on the last whitespace in the text's second half when there is one, else at the width."""
    if len(text) <= width: return {'text': text}
    if width <= 0: return {'text': '', 'text_truncated': True}
    keep = width - 1
    if not text[keep].isspace():
        space = max(text.rfind(c, 0, keep) for c in ' \n\t\r')
        if space >= keep // 2: keep = space
    return {'text': text[:keep].rstrip() + ELLIPSIS, 'text_truncated': True}
