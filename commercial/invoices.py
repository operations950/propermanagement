"""Tenant invoices: made when a month's rent posts (about 10 days before it is due), kept as they were made, shown as a PDF, and emailed from admin@ when someone says so
(or by itself, when the property is set to)."""
import io
from decimal import Decimal

from django.core.mail import EmailMessage
from django.utils import timezone

from accounting import board_send, ledger
from accounting.models import Charge

from .models import TenantInvoice

ZERO = Decimal('0.00')


class InvoiceError(Exception):
    pass


def _number(lease, month):
    return f'{lease.pk:04d}-{month:%Y%m}'


def _snapshot(lease, month):
    """(lines, previous balance, total) as the books stand now for this tenant and month."""
    rows = list(Charge.objects.filter(unit=lease.unit, owner=lease.tenant, is_void=False).order_by('due_date', 'id').prefetch_related('applications'))
    lines = [{'description': c.description, 'amount': str(c.amount)} for c in rows if c.due_date == month]
    previous = sum((c.balance for c in rows if c.due_date < month and c.balance > 0), ZERO)
    total = sum((Decimal(l['amount']) for l in lines), ZERO) + previous
    return lines, previous, total


def ensure_invoice(lease, month):
    """The tenant's invoice for a month: made if it is not there yet. Returns (invoice, made)."""
    month = month.replace(day=1)
    existing = TenantInvoice.objects.filter(lease=lease, month=month).first()
    if existing is not None:
        return existing, False
    lines, previous, total = _snapshot(lease, month)
    if not lines:
        raise InvoiceError('There is nothing posted for that month to invoice.')
    inv = TenantInvoice.objects.create(property=lease.property, lease=lease, month=month, due_date=month, number=_number(lease, month), tenant_name=lease.tenant.name,
                                       unit_label=lease.unit.label, lines=lines, previous_balance=previous, total=total)
    return inv, True


def refresh_invoice(inv):
    """Take the figures again from the books (only while it has not been sent)."""
    if inv.status != inv.Status.READY:
        raise InvoiceError('An invoice that was sent stays as it was sent.')
    lines, previous, total = _snapshot(inv.lease, inv.month)
    inv.lines, inv.previous_balance, inv.total = lines, previous, total
    inv.save(update_fields=['lines', 'previous_balance', 'total'])
    return inv


def landlord_name(prop):
    profile = ledger.profile_for(prop)
    return profile.legal_name or prop.name


def payment_instructions(prop):
    from .rent import settings_for
    return (settings_for(prop).payment_instructions or '').strip()


def pdf_bytes(inv):
    """The invoice as a one-page PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    prop = inv.property
    styles = getSampleStyleSheet()
    body = ParagraphStyle('b', parent=styles['Normal'], fontSize=10, leading=13)
    small = ParagraphStyle('s', parent=body, fontSize=8.5, textColor=colors.HexColor('#555555'))
    big = ParagraphStyle('h', parent=styles['Heading1'], fontSize=22, spaceAfter=2)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.8 * inch, bottomMargin=0.8 * inch, title=f'Invoice {inv.number}')
    story = [Paragraph('INVOICE', big), Paragraph(landlord_name(prop), body), Paragraph(prop.address or '', small), Spacer(1, 14)]
    head = Table([[Paragraph(f'<b>Bill to</b><br/>{inv.tenant_name}<br/>{prop.name}, unit {inv.unit_label}', body),
                   Paragraph(f'<b>Invoice</b> {inv.number}<br/><b>Date</b> {inv.created_at:%B} {inv.created_at.day}, {inv.created_at.year}<br/><b>Due</b> {inv.due_date:%B} {inv.due_date.day}, {inv.due_date.year}', body)]],
                 colWidths=[3.4 * inch, 3.4 * inch])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [head, Spacer(1, 18)]
    rows = [['Description', 'Amount']] + [[l['description'], f'${Decimal(l["amount"]):,.2f}'] for l in inv.lines]
    if inv.previous_balance:
        rows.append(['Previous balance', f'${inv.previous_balance:,.2f}'])
    rows.append(['Total due', f'${inv.total:,.2f}'])
    table = Table(rows, colWidths=[5.2 * inch, 1.6 * inch])
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef1f6')), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                               ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), ('LINEABOVE', (0, -1), (-1, -1), 0.8, colors.HexColor('#444444')),
                               ('LINEBELOW', (0, 0), (-1, -2), 0.25, colors.HexColor('#dddddd')), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    story.append(table)
    note = payment_instructions(prop)
    if note:
        story += [Spacer(1, 18), Paragraph('<b>How to pay</b>', body)] + [Paragraph(line, body) for line in note.splitlines() if line.strip()]
    doc.build(story)
    return buf.getvalue()


def filename(inv):
    return f'Invoice {inv.number} {inv.tenant_name}.pdf'.replace('/', '-')


def default_subject(inv):
    return f'Invoice {inv.number} for {inv.property.name}: due {inv.due_date:%B} {inv.due_date.day}'


def default_body(inv):
    return (f'Hello,\n\nAttached is the invoice for {inv.tenant_name}, unit {inv.unit_label}, at {inv.property.name}. It is due {inv.due_date:%B} {inv.due_date.day}, {inv.due_date.year}.\n\n'
            f'Please let me know if you have any questions.\n\nThank you,\n{board_send.SIGN_OFF}')


def send_invoice(inv, user=None, again=False):
    """Email the invoice to the lease's billing addresses. Refused (and nothing recorded) when it was already sent and `again` is not set, when there is no address, or when no
    mail delivery is set up."""
    if inv.status == inv.Status.SENT and not again:
        raise InvoiceError('That invoice was already sent. Choose "Send again" to send it a second time.')
    emails = [e for e in (board_send.valid_email(x) for x in inv.lease.billing_list()) if e]
    if not emails:
        raise InvoiceError(f'{inv.tenant_name} has no email address for invoices. Add one on the lease.')
    if not board_send.delivery_available():
        raise InvoiceError('No email delivery is set up yet: connect a Gmail mailbox in Admin Tools. Nothing was sent.')
    mail = EmailMessage(default_subject(inv), default_body(inv), board_send.FROM_EMAIL, to=emails)
    mail.attach(filename(inv), pdf_bytes(inv), 'application/pdf')
    try:
        sent = mail.send(fail_silently=False)
    except Exception as e:
        raise InvoiceError(f'The email could not be sent ({e}). Nothing was recorded.')
    if sent != 1:
        raise InvoiceError('The email could not be sent. Nothing was recorded; try again.')
    inv.status, inv.sent_at, inv.sent_by, inv.sent_to = inv.Status.SENT, timezone.now(), user, emails
    inv.send_count += 1
    inv.save(update_fields=['status', 'sent_at', 'sent_by', 'sent_to', 'send_count'])
    return inv
