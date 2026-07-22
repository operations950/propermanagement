from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from tickets.models import TicketAttachment


class VendorPhotoUploadForm(forms.ModelForm):
    class Meta:
        model = TicketAttachment
        fields = ['file', 'caption']

    def clean_file(self):
        file = self.cleaned_data['file']
        if file.content_type not in settings.VENDOR_UPLOAD_ALLOWED_CONTENT_TYPES:
            raise ValidationError('Only photo uploads (JPEG, PNG, WEBP, HEIC) are allowed.')
        if file.size > settings.VENDOR_UPLOAD_MAX_BYTES:
            max_mb = settings.VENDOR_UPLOAD_MAX_BYTES // (1024 * 1024)
            raise ValidationError(f'File is too large (max {max_mb}MB).')
        return file


class VendorCompleteForm(forms.Form):
    resolution_notes = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4}), required=False,
        label='Notes about what you did (optional)',
    )
