"""
Rules for a bounding box that arrives as four CSV cells (upload and update pipelines).

They match what the annotator API enforces (api/serializers.py): a box is given as fractions
of the image, all four values or none, with the box inside the image. The pipelines write to
the model directly and so do not pass through that serializer; this keeps the two paths equal.
"""
import math

BOX_COLUMNS = ("bbox_x", "bbox_y", "bbox_width", "bbox_height")

# A box that reaches the edge can overshoot by rounding (0.8 + 0.2000001).
TOLERANCE = 0.0001


def is_blank(value):
    """True for an empty cell: None, '', whitespace, NaN or the text 'nan'."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in ("", "nan")


def parse_box(x, y, width, height):
    """
    Turn four cells into a box.

    Returns (box, error): box is (x, y, width, height) as floats, or None when all four cells
    are blank (no box); error is a sentence saying what is wrong, or None.
    """
    raw = dict(zip(BOX_COLUMNS, (x, y, width, height)))
    blank = [name for name, value in raw.items() if is_blank(value)]
    if len(blank) == len(BOX_COLUMNS):
        return None, None
    if blank:
        return None, f"a box needs all four of {', '.join(BOX_COLUMNS)}; missing {', '.join(blank)}"

    numbers = {}
    for name, value in raw.items():
        try:
            number = float(str(value).strip())
        except ValueError:
            return None, f"{name} '{value}' is not a number (use a dot for decimals, e.g. 0.25)"
        if not math.isfinite(number):
            return None, f"{name} '{value}' is not a number"
        numbers[name] = number

    bx, by, bw, bh = (numbers[name] for name in BOX_COLUMNS)
    if any(v > 1 + TOLERANCE for v in (bx, by, bw, bh)):
        return None, (
            "box values are fractions of the image between 0 and 1, not pixels "
            f"(got {bx:g}, {by:g}, {bw:g}, {bh:g}); divide by the image width and height"
        )
    if bx < 0 or by < 0:
        return None, f"bbox_x and bbox_y must be between 0 and 1 (got {bx:g}, {by:g})"
    if bw <= 0 or bh <= 0:
        return None, f"bbox_width and bbox_height must be above 0 (got {bw:g}, {bh:g})"
    if bx + bw > 1 + TOLERANCE:
        return None, f"the box extends past the right edge (bbox_x + bbox_width = {bx + bw:g})"
    if by + bh > 1 + TOLERANCE:
        return None, f"the box extends past the bottom edge (bbox_y + bbox_height = {by + bh:g})"
    return (bx, by, bw, bh), None
