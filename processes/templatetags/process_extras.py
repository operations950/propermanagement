from django import template

register = template.Library()


@register.simple_tag
def record_options(target):
    """Candidate records for a RECORD_SELECTOR step's plain <select> —
    deliberately a simple select rather than the drilldown bubble-picker
    used elsewhere, since this one partial has to serve three very
    different target models from one generic step type."""
    from core.models import Contact, Property
    from tickets.models import Ticket

    if target == 'contact':
        return [(c.pk, str(c)) for c in Contact.objects.order_by('name')[:500]]
    if target == 'ticket':
        return [(t.pk, t.title) for t in Ticket.objects.order_by('-created_at')[:200]]
    return [(p.pk, p.name) for p in Property.objects.filter(is_active=True).order_by('name')]


@register.filter
def evaluate_formula(step):
    """Tiny, safe arithmetic evaluator for a CALCULATION_FORMULA step —
    substitutes other steps' numeric response values by step_key (e.g.
    "{late_fee} + {balance} * 1.05") and evaluates using Python's ast
    module restricted to numbers and +-*/(), never eval()."""
    import ast
    import operator

    formula = step.config.get('formula', '')
    if not formula:
        return ''

    values = {}
    for sibling in step.run.steps.all():
        val = (sibling.response or {}).get('value')
        if isinstance(val, (int, float)):
            values[sibling.step_key] = val

    try:
        expr = formula.format(**{k: v for k, v in values.items()})
    except KeyError:
        return 'Waiting on an earlier step'
    except (ValueError, IndexError):
        # A malformed formula (stray/unbalanced brace, an empty or
        # positional "{}" placeholder) — str.format() raises these for
        # bad TEMPLATE SYNTAX, as opposed to KeyError above (valid syntax,
        # just referencing a step_key that hasn't answered yet). Either
        # way this filter renders on ticket/property/contact detail pages
        # for every viewer, not just whoever wrote the formula, so a typo
        # here must degrade to a message, never a 500.
        return 'Invalid formula — check for a stray { or }'

    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg,
    }

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError('Unsupported expression')

    try:
        result = _eval(ast.parse(expr, mode='eval'))
    except (ValueError, SyntaxError, TypeError, ZeroDivisionError):
        return 'Could not calculate'
    return f'{result:,.2f}'


@register.filter
def step_response_summary(step):
    """One-line human-readable summary of a completed step's response —
    shown in place of the input UI once a step is done."""
    r = step.response or {}
    if step.step_type == 'checkbox':
        return 'Done'
    if step.step_type == 'checklist':
        items = r.get('checked_items', [])
        return f'{len(items)} item(s) checked' if items else 'Done'
    if step.step_type in ('short_text', 'long_text'):
        return r.get('text', '') or 'Done'
    if step.step_type == 'number_currency':
        value = r.get('value')
        return f'{value:,.2f}' if isinstance(value, (int, float)) else 'Done'
    if step.step_type == 'date_time':
        return r.get('value', '') or 'Done'
    if step.step_type == 'dropdown_multiselect':
        selected = r.get('selected', [])
        return ', '.join(selected) if selected else 'Done'
    if step.step_type == 'record_selector':
        return f'{r.get("model", "record")} #{r.get("id", "")}'
    if step.step_type == 'digital_signature':
        return 'Signed'
    if step.step_type == 'task_assignment':
        return f'Task #{r.get("ticket_id", "")}'
    if step.step_type == 'calendar_event':
        return r.get('event_datetime', '') or 'Scheduled'
    if step.step_type == 'approval_decision':
        return r.get('decision', '') or 'Decided'
    if step.step_type == 'email_text_action':
        return r.get('note', '') or 'Sent'
    if step.step_type == 'wait_timer':
        return 'Resumed'
    return 'Done'


@register.filter
def get_item(d, key):
    return d.get(key) if isinstance(d, dict) else None


@register.simple_tag
def route_value(routes, index, field):
    """Prefills one approval-route config row — routes is a list of
    {'label', 'action', 'target_step_key'} dicts; returns '' past the end
    of the list instead of raising, so the builder can always render a
    fixed number of empty/prefilled rows."""
    routes = routes or []
    if index >= len(routes):
        return ''
    return routes[index].get(field, '') or ''


@register.filter
def lines_join(items):
    """Joins a list with real newlines — Django template filter arguments
    don't interpret \\n as an escape, so |join:"\n" would literally print
    backslash-n instead of breaking lines."""
    return '\n'.join(items or [])
