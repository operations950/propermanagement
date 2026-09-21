"""A small JSON API for the message-answering assistant (see BOT_API.md).

    Authorization: Bearer pmk_...     (a BotAccessKey, made in Admin Tools)

    GET  /api/v1/me/                               who this key is and what it may do
    GET  /api/v1/properties/?q=                    find a property (name, address, unit, platform listing title)
    GET  /api/v1/properties/<id>/                  the profile; ?format=text gives a prompt-ready document
    GET  /api/v1/properties/<id>/faq/?q=           the FAQ (search words)
    POST /api/v1/properties/<id>/faq/              add or correct an entry
    PATCH/DELETE /api/v1/properties/<id>/faq/<n>/  correct or archive one of its own unreviewed entries
    POST /api/v1/properties/<id>/faq/<n>/used/     count a use

Only a Bearer key is accepted — never a browser session — so these views are exempt
from CSRF without being exposed to it. Responses are never cached. Codes and
internal notes are withheld unless the key was granted them."""
import json
from functools import wraps

from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import faq as faq_service
from .models import BotAccessKey, Property, PropertyFAQ, Unit
from .property_profile import build_profile, faq_entry, faq_for, render_text

MAX_BODY = 32 * 1024


def _error(status, code, message, **extra):
    resp = JsonResponse({'error': code, 'message': message, **extra}, status=status)
    if status == 401:
        resp['WWW-Authenticate'] = 'Bearer realm="proptasks"'
    return resp


def _authenticate(request):
    header = request.headers.get('Authorization', '')
    if not header.lower().startswith('bearer '):
        return None
    raw = header[7:].strip()
    if not raw:
        return None
    key = BotAccessKey.objects.filter(key_hash=BotAccessKey.hash_key(raw)).first()
    if key is None or not key.is_active:
        return None
    BotAccessKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now(), use_count=F('use_count') + 1)
    return key


def bot_api(methods, needs=None):
    """Bearer-key authentication, method check, permission check, no-store."""
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                resp = _error(405, 'method_not_allowed', f'Use {", ".join(methods)}.')
                resp['Allow'] = ', '.join(methods)
            else:
                key = _authenticate(request)
                if key is None:
                    resp = _error(401, 'unauthorized', 'A valid Bearer API key is required.')
                elif needs and not getattr(key, needs):
                    resp = _error(403, 'forbidden', 'This key is not allowed to do that.')
                else:
                    request.bot_key = key
                    resp = view(request, *args, **kwargs)
            resp['Cache-Control'] = 'no-store'
            return resp
        return wrapper
    return decorate


def _body(request):
    if len(request.body) > MAX_BODY:
        raise ValueError('Request body too large.')
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        raise ValueError('The body must be JSON.') from None
    if not isinstance(data, dict):
        raise ValueError('The body must be a JSON object.')
    return data


def _property(pk):
    return Property.objects.filter(pk=pk).prefetch_related('units', 'faqs').first()


@bot_api(['GET'])
def me(request):
    key = request.bot_key
    return JsonResponse({
        'name': key.name,
        'can': {
            'read_facts': True, 'read_access_info': key.allow_access_info,
            'read_internal_info': key.allow_internal_info, 'write_faq': key.allow_faq_write,
        },
    })


@bot_api(['GET'])
def property_search(request):
    """Find a property from whatever the message gives you: part of its name or
    address, a unit label, or the listing title from Airbnb/VRBO."""
    qs = Property.objects.all()
    if request.GET.get('include_inactive') != '1':
        qs = qs.filter(is_active=True)
    for token in request.GET.get('q', '').split():
        qs = qs.filter(
            Q(name__icontains=token) | Q(address__icontains=token) | Q(units__label__icontains=token)
            | Q(listing_names__name__icontains=token),
        )
    tokens = [t.lower() for t in request.GET.get('q', '').split()]
    props = qs.distinct().order_by('name').prefetch_related('units', 'listing_names')[:50]
    return JsonResponse({'properties': [
        {
            'id': p.pk, 'name': p.name, 'address': p.address, 'type': p.property_type, 'active': p.is_active,
            'units': [{'id': u.pk, 'label': u.label} for u in p.units.all() if u.is_active],
            'matched_unit': _matched_unit(p, tokens),
        }
        for p in props
    ]})


def _matched_unit(prop, tokens):
    """The one unit the search words point at — through the unit's label or one of its
    own platform listing titles — or None when they name the whole building or don't
    single a unit out. So a listing title like "800 Tropic - Wave (C)" finds Wave."""
    if not tokens:
        return None
    building = ' '.join([prop.name, prop.address] + [ln.name for ln in prop.listing_names.all() if not ln.unit_id]).lower()
    hits = []
    for unit in prop.units.all():
        if not unit.is_active:
            continue
        own = ' '.join([unit.label] + [ln.name for ln in prop.listing_names.all() if ln.unit_id == unit.pk]).lower()
        if any(t in own for t in tokens) and all(t in own or t in building for t in tokens):
            hits.append(unit)
    return {'id': hits[0].pk, 'label': hits[0].label} if len(hits) == 1 else None


@bot_api(['GET'])
def property_profile(request, pk):
    prop = _property(pk)
    if prop is None:
        return _error(404, 'not_found', 'No such property.')
    key = request.bot_key
    try:
        unit = _unit_from(prop, request.GET.get('unit_id'))
    except faq_service.FAQError as err:
        return _faq_error(err)
    profile = build_profile(prop, access=key.allow_access_info, internal=key.allow_internal_info, unit=unit)
    if request.GET.get('format') == 'text':
        return HttpResponse(render_text(profile), content_type='text/plain; charset=utf-8')
    return JsonResponse(profile)


def _faq_queryset(prop, request):
    """The FAQ, searched by words. With unit_id: the whole property's answers plus that
    unit's (not another unit's)."""
    unit = _unit_from(prop, request.GET.get('unit_id'))
    qs = faq_for(prop, unit)
    for token in request.GET.get('q', '').split():
        qs = qs.filter(Q(question__icontains=token) | Q(answer__icontains=token))
    return qs.order_by('question')


def _unit_from(prop, value):
    if value in (None, '', 0):
        return None
    unit = Unit.objects.filter(pk=value, property=prop, is_active=True).first() if str(value).isdigit() else None
    if unit is None:
        raise faq_service.FAQError('bad_unit', 'That unit does not belong to this property.', status=422)
    return unit


def _faq_error(err):
    extra = {'existing': faq_entry(err.existing)} if err.existing is not None else {}
    return _error(err.status, err.code, str(err), **extra)


@bot_api(['GET', 'POST'])
def faq_collection(request, pk):
    prop = _property(pk)
    if prop is None:
        return _error(404, 'not_found', 'No such property.')
    if request.method == 'GET':
        try:
            entries = _faq_queryset(prop, request)
        except faq_service.FAQError as err:
            return _faq_error(err)
        return JsonResponse({'property_id': prop.pk, 'faq': [faq_entry(e) for e in entries]})
    if not request.bot_key.allow_faq_write:
        return _error(403, 'forbidden', 'This key is not allowed to write FAQ entries.')
    try:
        data = _body(request)
        unit = _unit_from(prop, data.get('unit_id'))
        entry, created = faq_service.bot_write(
            prop, str(data.get('question', '')), str(data.get('answer', '')), request.bot_key, unit=unit,
            basis=str(data.get('basis', '')), source_note=str(data.get('source_note', '')),
        )
    except ValueError as err:
        if isinstance(err, faq_service.FAQError):
            return _faq_error(err)
        return _error(400, 'bad_request', str(err))
    payload = {'created': created, 'entry': faq_entry(entry)}
    similar = faq_service.similar(prop, entry.question_key, unit=entry.unit, exclude_pk=entry.pk)
    if similar:
        payload['similar'] = [{'id': s.pk, 'question': s.question} for s in similar]
    return JsonResponse(payload, status=201 if created else 200)


@bot_api(['GET', 'PATCH', 'DELETE'])
def faq_item(request, pk, faq_id):
    prop = _property(pk)
    entry = PropertyFAQ.objects.filter(pk=faq_id, property_id=pk, status=PropertyFAQ.Status.ACTIVE).select_related('unit', 'property').first()
    if prop is None or entry is None:
        return _error(404, 'not_found', 'No such FAQ entry.')
    if request.method == 'GET':
        return JsonResponse({'entry': faq_entry(entry)})
    if not request.bot_key.allow_faq_write:
        return _error(403, 'forbidden', 'This key is not allowed to change FAQ entries.')
    try:
        if request.method == 'DELETE':
            faq_service.bot_archive(entry)
            return JsonResponse({'archived': True, 'id': entry.pk})
        data = _body(request)
        entry = faq_service.bot_edit(
            entry, request.bot_key, question=data.get('question'), answer=data.get('answer'),
            basis=data.get('basis'), source_note=data.get('source_note'),
        )
    except ValueError as err:
        if isinstance(err, faq_service.FAQError):
            return _faq_error(err)
        return _error(400, 'bad_request', str(err))
    return JsonResponse({'entry': faq_entry(entry)})


@bot_api(['POST'])
def faq_used(request, pk, faq_id):
    """Count that the assistant used this entry to answer someone, so staff can
    see which entries earn their keep."""
    entry = PropertyFAQ.objects.filter(pk=faq_id, property_id=pk, status=PropertyFAQ.Status.ACTIVE).first()
    if entry is None:
        return _error(404, 'not_found', 'No such FAQ entry.')
    faq_service.record_use(entry)
    return JsonResponse({'id': entry.pk, 'times_used': entry.times_used + 1})
