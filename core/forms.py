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
        fields = ['name', 'property_type', 'address', 'city', 'timezone', 'is_general', 'is_active', 'notes']
        labels = {
            'name': 'Name',
            'property_type': 'Type',
            'city': 'City',
            'is_general': 'General placeholder (not a specific address)',
            'is_active': 'Active',
        }
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }
        help_texts = {
            'is_general': (
                'Check this only for a business-line placeholder like "Associations (general)" — '
                'not a real property. Lets a ticket be scoped to a business line without a specific address.'
            ),
        }


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ['name', 'contact_type', 'trade', 'phone', 'email', 'property', 'notes']
        labels = {'name': 'Name', 'contact_type': 'Type', 'property': 'Property (optional)'}
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }
