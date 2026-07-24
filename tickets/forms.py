from datetime import datetime

from django import forms
from django.utils import timezone

from core.models import Contact, Property, PropertyAttribute, StaffProfile, property_dropdown_queryset
from .models import Frequency, Ticket, TicketContact, TicketTemplate


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


class TicketTemplateForm(forms.ModelForm):
    target_type = forms.ChoiceField(
        choices=TicketTemplate.TargetType.choices, label='Target type',
        help_text='What this rule applies to — drives which of the fields below matter.',
    )
    property_types = forms.MultipleChoiceField(
        choices=Property.Type.choices, required=False,
        label='Restrict to property types (optional)',
        help_text='Only used when Target type is "Property category".',
    )
    default_assigned_role = forms.ChoiceField(
        choices=StaffProfile.Role.choices, label='Department',
        help_text='Every recurring task belongs to a department first — a specific person can be assigned within it.',
    )
    next_run_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text='The next date this task should be generated for.',
    )

    class Meta:
        model = TicketTemplate
        fields = [
            'title', 'description', 'target_type', 'property', 'property_types', 'contact',
            'required_attributes', 'frequency', 'workday_of_month', 'next_run_date',
            'default_assigned_role', 'default_assigned_staff', 'default_priority',
            'lead_time_days', 'requires_approval', 'approval_role', 'escalation_threshold_days',
            'is_active', 'skip_missed',
        ]
        labels = {
            'title': 'Title',
            'property': 'Specific property',
            'contact': 'Contact',
            'required_attributes': 'Requires these attributes (optional)',
            'default_assigned_staff': 'Specific person (optional)',
            'default_priority': 'Priority',
            'workday_of_month': 'Workday of month',
            'lead_time_days': 'Generate this many days early (optional)',
            'escalation_threshold_days': 'Flag overdue after this many days (optional)',
        }
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }
        help_texts = {
            'property': 'Only used when Target type is "Specific property".',
            'contact': 'Only used when Target type is "Contact" — applies to every property currently '
                       'linked to this contact.',
            'workday_of_month': 'Only used when frequency is "Monthly (by working day)".',
            'requires_approval': 'Completed instances need sign-off from the department below before counting as done.',
            'escalation_threshold_days': 'Flags (doesn\'t reassign) an overdue instance for visibility.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['property'].queryset = property_dropdown_queryset()
        self.fields['required_attributes'].queryset = PropertyAttribute.objects.filter(is_active=True)
        # Only contact types that plausibly anchor a recurring operational
        # rule — vendors/guests/tenants/staff-adjacent don't.
        self.fields['contact'].queryset = Contact.objects.filter(
            contact_type__in=[
                Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER, Contact.ContactType.ASSOCIATION_MEMBER,
            ],
        )
        self.fields['contact'].required = False
        if self.instance and self.instance.pk:
            self.initial['property_types'] = self.instance.property_types

    def clean_property_types(self):
        return list(self.cleaned_data['property_types'])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('frequency') == Frequency.MONTHLY_WORKDAY and not cleaned.get('workday_of_month'):
            self.add_error('workday_of_month', 'Required for "Monthly (by working day)" frequency.')
        if cleaned.get('requires_approval') and not cleaned.get('approval_role'):
            self.add_error('approval_role', 'Required when approval is needed.')
        if cleaned.get('target_type') == TicketTemplate.TargetType.PROPERTY and not cleaned.get('property'):
            self.add_error('property', 'Required when Target type is "Specific property".')
        if cleaned.get('target_type') == TicketTemplate.TargetType.CONTACT and not cleaned.get('contact'):
            self.add_error('contact', 'Required when Target type is "Contact".')
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
