"""Layered property-applicability rules for recurring task templates.

Computed live everywhere — no caching/materialization. Real data volume is
trivial (dozens of properties/templates), and a cache would need
invalidation on every template/attribute/property-attribute/package/
override edit for no measurable win at this scale. If real usage grows by
a couple of orders of magnitude, revisit.

`TicketTemplate.property` (a single FK) stays highest-precedence: if set,
the template applies to that one property only, full stop — this is what
keeps every existing template's behavior unchanged. The layered rules
below (property_types / required_attributes / package assignment) only
ever apply when `property` is blank.
"""
from core.models import Property

from ..models import PropertyTemplateOverride, TaskPackageTemplate, TicketTemplate


def _property_attribute_ids(property):
    return set(property.attribute_assignments.values_list('attribute_id', flat=True))


def _override_for(template, property):
    return PropertyTemplateOverride.objects.filter(template=template, property=property).first()


def template_applies_to_property(template, property, *, respect_overrides=True, override=None):
    """Pure predicate, no DB writes.

    Precedence: an EXCLUDE override always wins. Otherwise: template.property
    (exact match only) if set; else (property_type constraint AND required
    attributes) OR active package assignment. An INCLUDE override then
    force-includes regardless of the base match.
    """
    if respect_overrides:
        if override is None:
            override = _override_for(template, property)
        if override and override.action == PropertyTemplateOverride.Action.EXCLUDE:
            return False
    else:
        override = None

    if template.property_id:
        base_match = template.property_id == property.id
    else:
        package_match = TaskPackageTemplate.objects.filter(
            template=template, package__is_active=True,
            package__property_assignments__property=property,
        ).exists()
        has_type_or_attribute_constraint = bool(template.property_types) or template.required_attributes.exists()
        is_package_step = TaskPackageTemplate.objects.filter(template=template).exists()
        if is_package_step and not has_type_or_attribute_constraint:
            # A template with no property_types/required_attributes of its own,
            # that's ALSO a step in at least one package, is meant to be reached
            # only through that package's property assignments — not through the
            # "empty property_types = every type" default below, which would
            # otherwise silently fan it out to every property regardless of
            # package membership (that default exists for genuinely unconstrained
            # templates like "Fire extinguisher inspection", which aren't package
            # steps at all).
            base_match = package_match
        else:
            type_match = not template.property_types or property.property_type in template.property_types
            required_ids = set(template.required_attributes.values_list('id', flat=True))
            attr_match = required_ids <= _property_attribute_ids(property)
            base_match = (type_match and attr_match) or package_match

    if override and override.action == PropertyTemplateOverride.Action.INCLUDE:
        return True
    return base_match


def effective_templates_for_property(property):
    """Every active template matched against this one property — used by
    the property recurring-task review screen."""
    overrides = {o.template_id: o for o in PropertyTemplateOverride.objects.filter(property=property)}
    templates = TicketTemplate.objects.filter(is_active=True).prefetch_related('required_attributes')
    return [
        t for t in templates
        if template_applies_to_property(t, property, override=overrides.get(t.id))
    ]


def effective_properties_for_template(template):
    """Every active property matched against this one template — used by
    generation. Always runs every active property through
    template_applies_to_property rather than short-circuiting on
    template.property, so a PropertyTemplateOverride can still force-include
    or exclude a property even when the template is normally pinned to one
    specific property (e.g. "add this template as a one-off" on the
    property recurring-task review screen). When template.property is set
    and there are no overrides, the predicate naturally narrows this down
    to that one property — identical to the old short-circuit."""
    overrides = {o.property_id: o for o in PropertyTemplateOverride.objects.filter(template=template)}
    return [
        p for p in Property.objects.filter(is_active=True)
        if template_applies_to_property(template, p, override=overrides.get(p.id))
    ]


def effective_settings(template, property, override=None):
    """Effective frequency/workday_of_month/assigned_role/assigned_staff/
    priority for this template+property pair — override value if set, else
    the template default. Shared by generation (to apply) and the review
    screen (to display)."""
    if override is None:
        override = _override_for(template, property)
    return {
        'frequency': override.frequency if override and override.frequency else template.frequency,
        'workday_of_month': (
            override.workday_of_month if override and override.workday_of_month is not None
            else template.workday_of_month
        ),
        'assigned_role': override.assigned_role if override and override.assigned_role else template.default_assigned_role,
        'assigned_staff': (
            override.assigned_staff if override and override.assigned_staff_id
            else template.default_assigned_staff
        ),
        'priority': template.default_priority,
    }
