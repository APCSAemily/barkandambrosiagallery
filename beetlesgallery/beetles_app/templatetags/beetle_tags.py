from django import template

register = template.Library()

@register.simple_tag(takes_context=True)
def remove_filter(context, field, value=None):
    """
    Returns a URL query string with the specified field removed.
    Also resets pagination to page 1 to avoid 'empty page' errors.
    """
    query = context['request'].GET.copy()
    
    # If a specific value is provided, remove only that value from the list
    if value and field in query:
        values = query.getlist(field)
        if value in values:
            values.remove(value)
            # If values remain, update the list; otherwise delete the key
            if values:
                query.setlist(field, values)
            else:
                del query[field]
    # If no value provided, remove the entire key (fallback)
    elif field in query:
        del query[field]

    # Reset pagination
    if 'page' in query:
        del query['page']
        
    return query.urlencode()

@register.filter
def digit_groups(value):
    """
    Render a whole number with its digits in groups of three, each group a span with
    extra space before it (e.g. 70000 -> "70 000"), so large counts are easy to read.
    Non-numbers are returned unchanged.
    """
    from django.utils.safestring import mark_safe

    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    digits = str(abs(number))
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    gap = ' style="margin-left:0.4em"'
    spans = "".join(
        f'<span class="digit-group"{gap if i else ""}>{group}</span>'
        for i, group in enumerate(groups)
    )
    # Only digits and fixed markup go into the string, so it is safe to mark as such.
    return mark_safe(("-" if number < 0 else "") + spans)
