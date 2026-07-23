from datetime import datetime

from django import forms
from django.utils import timezone

from core.models import Contact, StaffProfile, property_dropdown_queryset
from .models import Ticket, TicketContact


class TicketForm(forms.ModelForm):
    reporter_contact = forms.ModelChoiceField(
        queryset=Contact.objects.all(), required=False,
        help_text='Who reported this? Used for one-click follow-up.',
    )
    assigned_role = forms.ChoiceField(
        choices=StaffProfile.Role.choices, label='Department',
        help_text='Every ticket belongs to a department first — a specific person can be assigned within it.',
    )
    due_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'}),
        help_text='Due dates are day-only — no time of day.',
    )

    class Meta:
        model = Ticket
        fields = [
            'title', 'description', 'property', 'priority', 'due_date',
            'assigned_role', 'assigned_staff', 'assigned_contact',
        ]
        labels = {'title': 'Title', 'property': 'Property (optional)'}
        widgets = {
            'title': forms.TextInput(attrs={'maxlength': 60}),
            'description': forms.Textarea(attrs={'rows': 3, 'maxlength': 200}),
        }
        help_texts = {
            'property': 'Leave blank (or pick a "general" option) if this isn\'t about one specific address.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same General -> Associations -> STR -> LTR -> Commercial clustering
        # used everywhere else properties are picked from.
        self.fields['property'].queryset = property_dropdown_queryset()

    def clean_due_date(self):
        # Converted to a timezone-aware midnight datetime *here*, in field
        # cleaning, not left as a bare date — ModelForm._post_clean() runs
        # instance.full_clean() as part of is_valid() (before the view ever
        # sees cleaned_data), so a bare date reaching the model's
        # DateTimeField at that point trips Django's naive-datetime
        # fallback (and its RuntimeWarning) regardless of what the view
        # does with it afterward.
        raw = self.cleaned_data.get('due_date')
        return timezone.make_aware(datetime.combine(raw, datetime.min.time())) if raw else None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('assigned_staff') and cleaned.get('assigned_contact'):
            raise forms.ValidationError('Assign to staff OR a vendor contact, not both.')
        return cleaned


class ReassignForm(forms.Form):
    assigned_role = forms.ChoiceField(choices=StaffProfile.Role.choices, label='Department')
    assigned_staff = forms.ModelChoiceField(
        queryset=StaffProfile.objects.all(), required=False,
        label='Specific person (optional)',
    )
    assigned_contact = forms.ModelChoiceField(
        queryset=Contact.objects.filter(contact_type=Contact.ContactType.VENDOR), required=False,
        label='Vendor / contractor (optional)',
    )
    note = forms.CharField(required=False, widget=forms.TextInput(attrs={'placeholder': 'Optional note'}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('assigned_staff') and cleaned.get('assigned_contact'):
            raise forms.ValidationError('Choose staff OR a vendor, not both — a role is required either way.')
        return cleaned


class FollowUpForm(forms.Form):
    channel = forms.ChoiceField(choices=[('email', 'Email'), ('sms', 'Text message')])
    to_override = forms.CharField(
        required=False, label='Send to (leave blank to use the reporter on file)',
    )
