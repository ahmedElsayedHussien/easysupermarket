from django import template

from apps.tenant.core.usernames import display_username as _display_username

register = template.Library()


@register.filter(name='display_username')
def display_username(value):
    """{{ user|display_username }} or {{ user.username|display_username }}:
    the login name without the internal tenant prefix."""
    return _display_username(value)


@register.filter(name='display_name')
def display_name(user):
    """Full name if set, otherwise the display username."""
    if not user:
        return ''
    try:
        full = user.get_full_name()
    except AttributeError:
        full = ''
    return full or _display_username(user)
