from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import ProcessAttachment


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
