from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import ProcessAttachment, ProcessRunExternalAccess


class ProcessAttachmentUploadForm(forms.ModelForm):
    """Proof-of-completion upload for a requires_upload checklist item —
    same clean_file() convention as vendorportal.forms.VendorPhotoUploadForm,
    just against a broader content-type allowlist (see
    settings.PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES — these are often
    scanned documents, not just photos)."""
    class Meta:
        model = ProcessAttachment
        fields = ['file', 'caption']
        widgets = {
            'caption': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Caption (optional)'}),
        }

    def clean_file(self):
        file = self.cleaned_data['file']
        if file.content_type not in settings.PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES:
            raise ValidationError('That file type isn\'t allowed — photos, PDFs, Word, or Excel files only.')
        if file.size > settings.PROCESS_ATTACHMENT_MAX_BYTES:
            max_mb = settings.PROCESS_ATTACHMENT_MAX_BYTES // (1024 * 1024)
            raise ValidationError(f'File is too large (max {max_mb}MB).')
        return file


class ProcessRunExternalAccessForm(forms.ModelForm):
    """Staff-facing "create a secure link" form — expiry is entered as a
    number of days from now rather than a raw datetime, matching how
    Ticket's own vendor-completion link expiry is set (see
    Ticket.rotate_completion_token/VENDOR_TOKEN_EXPIRY_DAYS)."""
    expires_in_days = forms.IntegerField(min_value=1, max_value=90, initial=7, required=False)

    class Meta:
        model = ProcessRunExternalAccess
        fields = ['external_contact']

    def save(self, commit=True):
        access = super().save(commit=False)
        days = self.cleaned_data.get('expires_in_days')
        access.token_expires_at = timezone.now() + timezone.timedelta(days=days) if days else None
        if commit:
            access.save()
        return access
