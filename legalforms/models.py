from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Contact, Property, Unit


class DocumentTemplate(models.Model):
    """A standardized form with legal or statutory wording that is filled from the system's records plus what a
    person types in (a notice of late assessment, an estoppel certificate, ...). `body` is the document's HTML with
    {{ merge fields }}; `fields` describes every input (see legalforms/catalog.py for the shape) — which the
    program pre-fills from what it knows and which the person completes. Editable by an admin (Django admin); the
    wording is what counsel reviews, so `statute` and `reviewed_note` say what it rests on and what was checked.
    `version` goes up every time the wording or the inputs change, and every generated document records the version
    it was made from."""
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=60, blank=True, help_text='Groups the list, e.g. "Collections", "Sales and transfers".')
    statute = models.CharField(max_length=200, blank=True, help_text='The statute the wording rests on, e.g. "§718.121(5), Florida Statutes".')
    description = models.TextField(blank=True)
    body = models.TextField(help_text='The document, as HTML with {{ merge fields }}.')
    fields = models.JSONField(default=list, help_text='Every input: key, label, type, prefill, default, required, help, remember.')
    companion_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='companions', help_text='For a document that goes with another (an affidavit of mailing goes with a notice): the one it goes with.')
    reviewed_note = models.TextField(blank=True, help_text='What still needs a lawyer or the board to confirm, or was last confirmed.')
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.pk:
            old = DocumentTemplate.objects.filter(pk=self.pk).values('body', 'fields').first()
            if old and (old['body'] != self.body or old['fields'] != self.fields):
                self.version += 1
        super().save(*args, **kwargs)


class GeneratedDocument(models.Model):
    """One document made from a template: the values that went into it and the exact text it came out as. The text is
    frozen when it is made — a document that was sent is a legal record, so it is never re-worked from live data or a
    later version of the wording. A draft can be changed; a final one cannot (make a revised copy instead)."""
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        FINAL = 'final', 'Final'

    template = models.ForeignKey(DocumentTemplate, on_delete=models.PROTECT, related_name='documents')
    template_version = models.PositiveIntegerField()
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name='legal_documents')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='legal_documents')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='companions', help_text='The document this one goes with (an affidavit -> its notice).')
    subject = models.CharField(max_length=200, blank=True, help_text='Who/what it is about, for the list: "Fernando Suzuki — Unit 32".')
    values = models.JSONField(default=dict)
    body_html = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    # Sending it (mainly for a notice: the affidavit swears to when it was mailed).
    mailed_on = models.DateField(null=True, blank=True)
    mail_method = models.CharField(max_length=60, blank=True, help_text='e.g. First class mail, Certified mail, Email.')
    tracking = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['property', '-created_at'])]

    def __str__(self):
        return f'{self.template.name} — {self.subject}' if self.subject else self.template.name

    def finalize(self):
        self.status = self.Status.FINAL
        self.finalized_at = timezone.now()
        self.save(update_fields=['status', 'finalized_at'])
