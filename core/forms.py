from django import forms

from tickets.models import PropertyTemplateOverride

from .models import Contact, Property, StaffProfile


class PropertyTemplateOverrideForm(forms.ModelForm):
    """Validates the property recurring-task review screen's per-row
    "adjust" action — every field is optional, since a blank field just
    means "use the template's default" (see tickets.services.applicability
    .effective_settings)."""
    class Meta:
        model = PropertyTemplateOverride
        fields = ['frequency', 'workday_of_month', 'assigned_role', 'assigned_staff']
        labels = {
            'frequency': 'Frequency override',
            'workday_of_month': 'Workday of month override',
            'assigned_role': 'Department override',
            'assigned_staff': 'Assignee override',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].required = False
        self.fields['assigned_staff'].queryset = StaffProfile.objects.select_related('user')


class PropertyForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = ['name', 'property_type', 'street', 'city', 'state', 'zip_code', 'is_general', 'is_active', 'notes']
        labels = {
            'name': 'Name',
            'property_type': 'Type',
            'street': 'Street',
            'city': 'City',
            'state': 'State',
            'zip_code': 'ZIP code',
            'is_general': 'General placeholder (not a specific address)',
            'is_active': 'Active',
        }
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
            'state': forms.TextInput(attrs={'maxlength': 2}),
        }
        help_texts = {
            'is_general': (
                'Check this only for a business-line placeholder like "Associations (general)" — '
                'not a real property. Lets a ticket be scoped to a business line without a specific address.'
            ),
        }

    def clean(self):
        cleaned = super().clean()
        # General placeholders ("Associations (general)", "No specific
        # property", ...) inherently have no real address — everyone else
        # gets the full street/city/state/zip requirement.
        if not cleaned.get('is_general'):
            for field in ('street', 'city', 'state', 'zip_code'):
                if not cleaned.get(field):
                    self.add_error(field, 'Required unless this is a general placeholder.')
        return cleaned


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ['name', 'contact_type', 'trade', 'phone', 'email', 'properties', 'notes']
        labels = {'name': 'Name', 'contact_type': 'Type', 'properties': 'Properties (optional)'}
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
            'phone': forms.TextInput(attrs={'type': 'tel', 'placeholder': '555-123-4567'}),
        }
