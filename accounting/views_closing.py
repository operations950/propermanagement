"""The two screens behind the association close sheet's reserve check and its last step, sending the financials to the board."""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from . import board_send, labels, package, reserve_check
from .web import admin_only, association_or_404, books_or_redirect, default_month, month_context, parse_month, acc_up


def _close_back(pk, first):
    return reverse('accounting:close', args=[pk, f'{first:%Y-%m}'])


@admin_only
def reserve(request, pk, month):
    """Every reserve bank account and every reserve fund in one place, the difference, and the acknowledgement of it."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    first = parse_month(month, default_month())
    here = reverse('accounting:reserve_check', args=[pk, f'{first:%Y-%m}'])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'acknowledge':
                reserve_check.acknowledge(prop, first, request.POST.get('reason'), request.user)
                messages.success(request, 'Difference acknowledged.')
            elif action == 'remove':
                reserve_check.remove(prop, first)
                messages.success(request, 'The acknowledgement is removed.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    info = reserve_check.check(prop, first)
    return render(request, 'accounting/reserve_check.html', {'assoc': prop, 'info': info, **acc_up(request, prop, _close_back(pk, first), 'Month-end close'), **month_context(first)})


@admin_only
def send_board(request, pk, month):
    """Review the package, see who it goes to, and send it. Once it is sent the step is complete."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    first = parse_month(month, default_month())
    here = reverse('accounting:send_board', args=[pk, f'{first:%Y-%m}'])
    rows = board_send.board_rows(prop)
    locked = board_send.is_locked(prop, first)
    sent = board_send.last_send(prop, first)
    subject, body = board_send.default_subject(prop, first), board_send.default_body(prop, first)
    if request.method == 'POST':
        subject = request.POST.get('subject') or subject
        body = request.POST.get('body') or body
        picked = set(request.POST.getlist('to'))
        chosen = [r for r in rows if str(r['key']) in picked]
        try:
            if not request.POST.get('approved'):
                raise board_send.BoardSendError('Tick "I have reviewed the package" before sending.')
            if sent is not None and not request.POST.get('again'):
                raise board_send.BoardSendError('This month\'s package was already sent. Choose "Send again" to send it a second time.')
            board_send.send(prop, first, chosen, subject, body, request.user)
            messages.success(request, 'Sent to the board.')
            return redirect(here)
        except board_send.BoardSendError as e:
            messages.error(request, str(e))
        except Exception as e:                               # the mail server said no: say so plainly, nothing was recorded
            messages.error(request, f'The email could not be sent ({e}). Nothing was recorded.')
        selected = {r['key'] for r in chosen}
    else:
        selected = {r['key'] for r in rows if r['email']}
    can_send = locked and any(r['email'] and r['key'] in selected for r in rows) and board_send.delivery_available()
    if not locked:
        why = f'Close {first:%B %Y} first: {labels.label(prop, "package_gets")} the package once the month is closed.'
    elif not any(r['email'] for r in rows):
        why = f'No {labels.label(prop, "package_person")} has an email address on file. Add one on the contact first.'
    elif not board_send.delivery_available():
        why = 'No email delivery is set up yet: connect a Gmail mailbox in Admin Tools.'
    else:
        why = ''
    for r in rows:
        r['picked'] = r['key'] in selected
    return render(request, 'accounting/send_board.html', {'assoc': prop, 'rows': rows, 'sent': sent, 'locked': locked, 'subject': subject, 'body': body, 'from_email': board_send.FROM_EMAIL,
                                                          'cc': board_send.CC_EMAILS, 'can_send': can_send, 'why_not': why, 'filename': package.file_name(prop, first),
                                                          'package_url': reverse('accounting:report_package', args=[pk]) + f'?month={first:%Y-%m}', **acc_up(request, prop, _close_back(pk, first), 'Month-end close'), **month_context(first)})
