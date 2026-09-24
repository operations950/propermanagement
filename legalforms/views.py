from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.safestring import mark_safe

from core.models import Property, StaffProfile, Unit
from core.views import _is_admin

from . import services
from .models import DocumentTemplate, GeneratedDocument


def _allowed(user):
    """Legal documents go out under the association's and the company's name: admins and property managers."""
    profile = getattr(user, 'staff_profile', None)
    return _is_admin(user) or (profile is not None and profile.role == StaffProfile.Role.PROPERTY_MANAGER)


def _guard(request):
    if not _allowed(request.user):
        raise PermissionDenied


def show_html(doc):
    """The frozen text, with the logo pointed at wherever it is served now."""
    return mark_safe(doc.body_html.replace(services.LOGO_TOKEN, static('img/proper-realty-logo.png')))


def _properties():
    return Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.ASSOCIATION).order_by('name')


def _units_json():
    out = {}
    for u in Unit.objects.filter(is_active=True, property__in=_properties()).order_by('label'):
        out.setdefault(str(u.property_id), []).append({'id': u.pk, 'label': u.label})
    return out


@login_required
def document_list(request):
    _guard(request)
    prop = None
    docs = GeneratedDocument.objects.select_related('template', 'property', 'unit', 'created_by')
    if request.GET.get('property', '').isdigit():
        prop = Property.objects.filter(pk=int(request.GET['property'])).first()
        if prop:
            docs = docs.filter(property=prop)
    templates = list(DocumentTemplate.objects.filter(is_active=True, companion_of__isnull=True))
    return render(request, 'legalforms/list.html', {'docs': docs[:200], 'property': prop, 'templates': templates})


def _target_from_query(request):
    prop = Property.objects.filter(pk=request.GET.get('property'), is_active=True).first() if request.GET.get('property', '').isdigit() else None
    unit = None
    if prop and request.GET.get('unit', '').isdigit():
        unit = Unit.objects.filter(pk=int(request.GET['unit']), property=prop).first()
    parent = GeneratedDocument.objects.filter(pk=request.GET['parent']).first() if request.GET.get('parent', '').isdigit() else None
    if parent:
        prop, unit = parent.property, parent.unit
    return prop, unit, parent


def _form_page(request, template, prop, unit, parent, values, errors, editing=None):
    return render(request, 'legalforms/form.html', {
        'template': template, 'property': prop, 'unit': unit, 'parent': parent, 'values': values, 'errors': errors,
        'fields': template.fields, 'editing': editing,
    })


@login_required
def document_new(request, slug):
    _guard(request)
    template = get_object_or_404(DocumentTemplate, slug=slug, is_active=True)
    prop, unit, parent = _target_from_query(request)
    if request.method == 'POST':
        prop = get_object_or_404(Property, pk=request.POST.get('property'))
        unit = Unit.objects.filter(pk=request.POST.get('unit'), property=prop).first() if request.POST.get('unit') else None
        parent = GeneratedDocument.objects.filter(pk=request.POST.get('parent')).first() if request.POST.get('parent') else None
        raw = {f['key']: request.POST.get('f_' + f['key'], '') for f in template.fields}
        doc, errors = services.save_document(template, prop, unit, request.user, raw, parent=parent)
        if doc:
            messages.success(request, f'{template.name} created as a draft. Read it through, then make it final.')
            return redirect('legalforms:detail', pk=doc.pk)
        return _form_page(request, template, prop, unit, parent, raw, errors)
    if prop is None:
        return render(request, 'legalforms/choose.html', {'template': template, 'properties': _properties(), 'units_json': _units_json()})
    values = services.initial_values(template, prop, unit, request.user, parent=parent)
    return _form_page(request, template, prop, unit, parent, values, {})


@login_required
def document_edit(request, pk):
    _guard(request)
    doc = get_object_or_404(GeneratedDocument.objects.select_related('template', 'property', 'unit'), pk=pk)
    if doc.status != GeneratedDocument.Status.DRAFT:
        messages.error(request, 'A final document cannot be changed — make a revised copy.')
        return redirect('legalforms:detail', pk=doc.pk)
    template = doc.template
    if request.method == 'POST':
        raw = {f['key']: request.POST.get('f_' + f['key'], '') for f in template.fields}
        saved, errors = services.save_document(template, doc.property, doc.unit, request.user, raw, parent=doc.parent, doc=doc)
        if saved:
            messages.success(request, 'Saved.')
            return redirect('legalforms:detail', pk=doc.pk)
        return _form_page(request, template, doc.property, doc.unit, doc.parent, raw, errors, editing=doc)
    return _form_page(request, template, doc.property, doc.unit, doc.parent, doc.values, {}, editing=doc)


@login_required
def document_detail(request, pk):
    _guard(request)
    doc = get_object_or_404(GeneratedDocument.objects.select_related('template', 'property', 'unit', 'parent', 'created_by'), pk=pk)
    return render(request, 'legalforms/detail.html', {
        'doc': doc, 'html': show_html(doc), 'companions': doc.template.companions.filter(is_active=True),
        'children': doc.companions.select_related('template'),
    })


@login_required
def document_print(request, pk):
    _guard(request)
    doc = get_object_or_404(GeneratedDocument, pk=pk)
    return render(request, 'legalforms/print.html', {'doc': doc, 'html': show_html(doc)})


@login_required
def document_act(request, pk):
    _guard(request)
    doc = get_object_or_404(GeneratedDocument.objects.select_related('template'), pk=pk)
    if request.method != 'POST':
        return redirect('legalforms:detail', pk=doc.pk)
    action = request.POST.get('action')
    if action == 'finalize' and doc.status == GeneratedDocument.Status.DRAFT:
        doc.finalize()
        messages.success(request, 'Final. It can no longer be changed; make a revised copy if something is wrong.')
    elif action == 'mailing':
        raw = (request.POST.get('mailed_on') or '').strip()
        try:
            doc.mailed_on = datetime.strptime(raw, '%Y-%m-%d').date() if raw else None
        except ValueError:
            messages.error(request, 'Enter the mailing date.')
            return redirect('legalforms:detail', pk=doc.pk)
        doc.mail_method = request.POST.get('mail_method', '').strip()[:60]
        doc.tracking = request.POST.get('tracking', '').strip()[:80]
        doc.save(update_fields=['mailed_on', 'mail_method', 'tracking'])
        messages.success(request, 'Mailing recorded.')
    elif action == 'revise':
        copy = services.revised_copy(doc, request.user)
        messages.success(request, 'A revised copy is open as a draft — change what is wrong and make it final.')
        return redirect('legalforms:edit', pk=copy.pk)
    elif action == 'delete' and doc.status == GeneratedDocument.Status.DRAFT:
        prop_pk = doc.property_id
        doc.delete()
        messages.success(request, 'Draft deleted.')
        return redirect(reverse('legalforms:list') + f'?property={prop_pk}')
    return redirect('legalforms:detail', pk=doc.pk)
