from django import forms

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

    class Meta:
        model = Ticket
        fields = [
            'title', 'description', 'property', 'priority', 'due_date',
            'assigned_role', 'assigned_staff', 'assigned_contact',
        ]
        labels = {'title': 'Short title (a few words, not a sentence)', 'property': 'Property (optional)'}
        widgets = {
            'due_date': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }
        help_texts = {
            'description': 'One short sentence — full details can go in a linked attachment or note.',
            'property': 'Leave blank (or pick a "general" option) if this isn\'t about one specific address.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same General -> Associations -> STR -> LTR -> Commercial clustering
        # used everywhere else properties are picked from.
        self.fields['property'].queryset = property_dropdown_queryset()

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
