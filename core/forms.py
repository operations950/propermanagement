from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm

from tickets.models import PropertyTemplateOverride

from .models import Contact, Property, StaffProfile


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    """Relabels the login form's identifier field as Email — new accounts
    log in with their email (see core.auth_backends), a few legacy accounts
    without one on file yet can still type their original username."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Email'
        self.fields['username'].widget.attrs.update({'autofocus': True, 'autocomplete': 'email'})


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


SECONDARY_TYPE_CHOICES = [c for c in Contact.ContactType.choices if c[0] != Contact.ContactType.VENDOR]


class ContactForm(forms.ModelForm):
    # secondary_types is a plain JSONField, not a relation — handled here as
    # a bound-but-not-Meta.fields MultipleChoiceField so ModelForm doesn't try
    # to auto-map it (its default JSONField widget is a raw text area), and
    # saved onto the instance explicitly in save() below instead.
    secondary_types = forms.MultipleChoiceField(
        choices=SECONDARY_TYPE_CHOICES, required=False, label='Also (optional)',
    )

    class Meta:
        model = Contact
        fields = ['name', 'contact_type', 'trade', 'phone', 'email', 'properties', 'notes']
        labels = {'name': 'Name', 'contact_type': 'Type', 'properties': 'Properties (optional)'}
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
            'phone': forms.TextInput(attrs={'type': 'tel', 'placeholder': '555-123-4567'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['secondary_types'].initial = self.instance.secondary_types

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('contact_type') == Contact.ContactType.VENDOR and not cleaned.get('trade'):
            self.add_error('trade', 'Choose a trade for vendor/contractor contacts.')
        # Vendor/Contractor stays single-type — a Trade already distinguishes it,
        # and it's never combined with e.g. Owner/Board Member in practice.
        if cleaned.get('contact_type') == Contact.ContactType.VENDOR:
            cleaned['secondary_types'] = []
        return cleaned

    def save(self, commit=True):
        contact = super().save(commit=False)
        contact.secondary_types = self.cleaned_data.get('secondary_types') or []
        if commit:
            contact.save()
            self.save_m2m()
        return contact


class StaffCreateForm(forms.Form):
    """Admin Tools' "New Staff" form — there's no other way to create a
    staff account short of Django admin. Deliberately a plain Form, not a
    ModelForm over User, since it spans two models (User + StaffProfile)
    and needs its own clean_email to check for a collision against an
    existing Contact (see core/views.py::staff_create for the confirm-
    before-merge flow that check enables)."""
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, required=False, label='Last name')
    email = forms.EmailField(label='Email')
    phone = forms.CharField(max_length=30, required=False, label='Phone (optional)')
    role = forms.ChoiceField(choices=StaffProfile.Role.choices, required=False, label='Department')
    timezone = forms.ChoiceField(
        choices=StaffProfile.Timezone.choices, initial=StaffProfile.Timezone.EASTERN, label='Timezone',
    )
    password = forms.CharField(widget=forms.PasswordInput, label='Temporary password')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('A user with this email already exists.')
        return email
