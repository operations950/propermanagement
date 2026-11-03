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
            'title', 'description', 'property', 'unit', 'priority', 'due_date',
            'assigned_role', 'assigned_staff', 'assigned_contact',
        ]
        labels = {'title': 'Title', 'property': 'Property (optional)', 'unit': 'Unit (optional)'}
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
        # super().clean() (BaseModelForm.clean()) just turned constraint
        # validation back on for _post_clean() — turn it back off.
        # ticket_exactly_one_assignee (tickets/models.py) is deliberately
        # allowed to be unsatisfied on a brand-new, department-only ticket:
        # assigned_role is the only required field on this form, and
        # Ticket.save()'s own auto-assign fallback (see its docstring) is
        # what's actually meant to guarantee a real assignee before the row
        # reaches the DB — not this pre-save form validation. Without this,
        # is_valid() rejects the ticket outright the moment both
        # assigned_staff/assigned_contact are left blank, before save() ever
        # gets a chance to run — the exact "Unassigned" flow the New Ticket
        # UI itself explicitly supports (assigned_staff/assigned_contact
        # aren't required fields here). Same fix as ticket_quick_edit/
        # ticket_reassign/ticket_assign_contractor's validate_constraints=
        # False, just at the form layer since this is the one assignment
        # path that goes through a ModelForm instead of a bare view.
        self._validate_constraints = False
        if cleaned.get('assigned_staff') and cleaned.get('assigned_contact'):
            raise forms.ValidationError('Assign to staff OR a vendor contact, not both.')
        unit = cleaned.get('unit')
        if unit and unit.property_id != (cleaned.get('property') and cleaned['property'].pk):
            raise forms.ValidationError('The selected unit doesn\'t belong to the selected property.')
        return cleaned


class ReassignForm(forms.Form):
    """Internal Reassign — department/staff only. Assigning a contractor
    is a separate action (see AssignContractorForm/ticket_assign_contractor)
    surfaced next to Related Contacts instead of here."""
    assigned_role = forms.ChoiceField(choices=StaffProfile.Role.choices, label='Department')
    assigned_staff = forms.ModelChoiceField(
        queryset=StaffProfile.objects.all(), required=False,
        label='Specific person (optional)',
    )


class AssignContractorForm(forms.Form):
    assigned_contact = forms.ModelChoiceField(
        queryset=Contact.objects.filter(contact_type=Contact.ContactType.VENDOR), required=False,
        label='Vendor / contractor',
    )
