"""The owner portal screens: create a login (email, password, then an emailed 6-digit code), sign in, forgot password, the year of released months, and
one released month's statement. Everything an owner reaches is checked against what they own: a rental that is not theirs, or a month that has not been
released, is simply "not found". The rules live in core/owner_portal.py; the wall that keeps owners off staff pages is core.middleware.OwnerWallMiddleware."""
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from vendorportal.models import AccessAttempt

from . import ledger, owner_portal
from .auth_backends import _client_ip
from .models import MonthRelease, OwnerAccount, OwnerCode

BACKEND = 'django.contrib.auth.backends.ModelBackend'
THROTTLED = 'Too many attempts from this connection. Please wait a few minutes and try again.'


def _throttled(request):
    return AccessAttempt.is_rate_limited(_client_ip(request), limit=20)


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not owner_portal.is_owner_user(request.user):
            return redirect('owner_login')
        account = OwnerAccount.objects.filter(user=request.user, is_active=True).first()
        if account is None:
            logout(request)
            messages.error(request, 'This login is not active. Please contact Proper Realty.')
            return redirect('owner_login')
        request.owner_account = account
        return view(request, *args, **kwargs)
    return wrapper


def _start_session(request, user):
    login(request, user, backend=BACKEND)
    request.session.set_expiry(owner_portal.SESSION_SECONDS)


def owner_login(request):
    if owner_portal.is_owner_user(request.user):
        return redirect('owner_home')
    email = ''
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        if _throttled(request):
            messages.error(request, THROTTLED)
        else:
            user, why = owner_portal.authenticate_owner(email, request.POST.get('password', ''))
            if user is not None:
                _start_session(request, user)
                return redirect('owner_home')
            messages.error(request, {
                'locked': 'Too many wrong passwords. This login is locked for 15 minutes; you can also choose "Forgot your password?".',
                'disabled': 'This login has been switched off. Please contact Proper Realty.',
            }.get(why, "That email and password don't match."))
    return render(request, 'core/owner/login.html', {'email': email})


def _password_form(request, purpose, template, title):
    """The sign-up and forgot-password forms are the same: an email, a new password twice; the code comes next."""
    email = ''
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        password, again = request.POST.get('password', ''), request.POST.get('password2', '')
        errors = []
        try:
            validate_email(email)
        except ValidationError:
            errors.append('Enter a valid email address.')
        if password != again:
            errors.append('The two passwords do not match.')
        else:
            try:
                validate_password(password)
            except ValidationError as exc:
                errors.extend(exc.messages)
        if not errors and _throttled(request):
            errors.append(THROTTLED)
        if not errors:
            sent = owner_portal.start_code(email, password, purpose)
            if sent is False:
                errors.append("We couldn't send the email just now. Please try again in a little while.")
            else:
                # the same page whether or not that address belongs to an owner, so nobody can use it to find out who does
                request.session['owner_pending'] = {'email': owner_portal.norm_email(email), 'purpose': purpose}
                return redirect('owner_verify')
        for error in errors:
            messages.error(request, error)
    return render(request, template, {'email': email, 'title': title})


def owner_signup(request):
    if owner_portal.is_owner_user(request.user):
        return redirect('owner_home')
    return _password_form(request, OwnerCode.Purpose.SIGNUP, 'core/owner/signup.html', 'Create your login')


def owner_forgot(request):
    return _password_form(request, OwnerCode.Purpose.RESET, 'core/owner/signup.html', 'Choose a new password')


def owner_verify(request):
    pending = request.session.get('owner_pending')
    if not pending:
        return redirect('owner_signup')
    if request.method == 'POST':
        if _throttled(request):
            messages.error(request, THROTTLED)
        else:
            status, account = owner_portal.verify_code(pending['email'], pending['purpose'], request.POST.get('code', ''))
            if status == 'ok':
                request.session.pop('owner_pending', None)
                _start_session(request, account.user)
                messages.success(request, 'You are in.' if pending['purpose'] == OwnerCode.Purpose.SIGNUP else 'Your password is changed.')
                return redirect('owner_home')
            messages.error(request, "That code isn't right, or it has expired. Check the latest email, or start again.")
    return render(request, 'core/owner/verify.html', {'email': pending['email'], 'minutes': owner_portal.CODE_MINUTES, 'reset': pending['purpose'] == OwnerCode.Purpose.RESET})


@require_POST
def owner_logout(request):
    logout(request)
    return redirect('owner_login')


def _property_for(request, pk):
    prop = next((p for p in owner_portal.visible_properties(request.owner_account.email) if p.pk == pk), None)
    if prop is None:
        raise Http404
    return prop


@owner_required
def owner_home(request):
    props = owner_portal.visible_properties(request.owner_account.email)
    if not props:
        return render(request, 'core/owner/home.html', {'props': [], 'account': request.owner_account})
    try:
        chosen = int(request.GET.get('property', ''))
    except ValueError:
        chosen = None
    prop = next((p for p in props if p.pk == chosen), props[0])
    all_releases = owner_portal.released_months(prop)
    years = sorted({r.month.year for r in all_releases})
    try:
        year = int(request.GET.get('year', ''))
    except ValueError:
        year = None
    if year not in years:
        year = years[-1] if years else None
    table = owner_portal.year_table([r for r in all_releases if r.month.year == year]) if year else None
    return render(request, 'core/owner/home.html', {'props': props, 'prop': prop, 'years': years, 'year': year, 'table': table, 'account': request.owner_account})


@owner_required
def owner_month(request, pk, month):
    prop = _property_for(request, pk)
    try:
        first = datetime.strptime(month, '%Y-%m').date().replace(day=1)
    except ValueError:
        raise Http404
    release = MonthRelease.objects.filter(property=prop, month=first).first()
    if release is None:
        raise Http404
    others = list(MonthRelease.objects.filter(property=prop).order_by('month').values_list('month', flat=True))
    i = others.index(first)
    return render(request, 'core/owner/month.html', {
        'prop': prop, 'release': release, 's': owner_portal.display(release.snapshot), 'month': first, 'account': request.owner_account,
        'earlier': others[i - 1] if i else None, 'later': others[i + 1] if i + 1 < len(others) else None,
    })
