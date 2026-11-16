from django import forms

from core.models import Contact, Unit

from .models import Lease


class LeaseForm(forms.ModelForm):
    """One lease. The tenant is typed by name (an existing tenant of this property with that name is used, else a new contact is made); a unit that is not on the property yet can be
    named here and is made."""
    tenant_name = forms.CharField(max_length=200, label='Tenant')
    tenant_email = forms.EmailField(required=False, label='Tenant email')
    tenant_phone = forms.CharField(max_length=30, required=False, label='Tenant phone')
    new_unit_label = forms.CharField(max_length=100, required=False, label='Or a new unit')
    new_unit_sqft = forms.IntegerField(required=False, min_value=0, label='Its square feet')

    class Meta:
        model = Lease
        fields = ['unit', 'status', 'start_date', 'end_date', 'base_rent', 'prorate', 'cam_estimate', 'security_deposit', 'deposit_received_on', 'deposit_note', 'billing_emails',
                  'renewal_notes', 'notes', 'late_fee_flat', 'late_fee_percent']
        widgets = {'start_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'), 'end_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'deposit_received_on': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'), 'billing_emails': forms.Textarea(attrs={'rows': 2}),
                   'renewal_notes': forms.Textarea(attrs={'rows': 2}), 'notes': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, prop=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.prop = prop
        self.fields['unit'].queryset = Unit.objects.filter(property=prop, is_active=True).order_by('label')
        self.fields['unit'].required = False
        self.fields['unit'].empty_label = 'Choose a unit'
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs['class'] = 'form-check-input'
            else:
                w.attrs['class'] = (w.attrs.get('class', '') + ' form-select form-select-sm' if isinstance(w, forms.Select) else w.attrs.get('class', '') + ' form-control form-control-sm').strip()
        if self.instance.pk:
            self.fields['tenant_name'].initial = self.instance.tenant.name
            self.fields['tenant_email'].initial = self.instance.tenant.email
            self.fields['tenant_phone'].initial = self.instance.tenant.phone

    def clean(self):
        data = super().clean()
        label = ' '.join((data.get('new_unit_label') or '').split())
        if not data.get('unit') and not label:
            raise forms.ValidationError('Choose the unit, or name a new one.')
        start, end = data.get('start_date'), data.get('end_date')
        if start and end and end < start:
            self.add_error('end_date', 'The lease cannot end before it starts.')
        if data.get('base_rent') is not None and data['base_rent'] < 0:
            self.add_error('base_rent', 'The rent cannot be negative.')
        return data

    def tenant_contact(self):
        """The tenant Contact: the one this lease already has when the name is unchanged, else an existing contact of this property with that name, else a new one."""
        name = ' '.join(self.cleaned_data['tenant_name'].split())
        email, phone = (self.cleaned_data.get('tenant_email') or '').strip(), (self.cleaned_data.get('tenant_phone') or '').strip()
        person = None
        if self.instance.pk and self.instance.tenant.name.lower() == name.lower():
            person = self.instance.tenant
        if person is None:
            person = (Contact.objects.filter(name__iexact=name, properties=self.prop).first() or Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.TENANT).first())
        if person is None:
            person = Contact.objects.create(name=name, contact_type=Contact.ContactType.TENANT, source=Contact.Source.MANUAL)
        changed = []
        if email and person.email != email:
            person.email = email
            changed.append('email')
        if phone and person.phone != phone:
            person.phone = phone
            changed.append('phone')
        if changed:
            person.save(update_fields=changed)
        return person

    def save(self, commit=True):
        lease = super().save(commit=False)
        lease.property = self.prop
        label = ' '.join((self.cleaned_data.get('new_unit_label') or '').split())
        if label:
            unit, made = Unit.objects.get_or_create(property=self.prop, label=label)
            if made and self.cleaned_data.get('new_unit_sqft'):
                unit.square_footage = self.cleaned_data['new_unit_sqft']
                unit.save(update_fields=['square_footage'])
            lease.unit = unit
        lease.tenant = self.tenant_contact()
        if lease.status == Lease.Status.ENDED and not lease.end_date:
            from django.utils import timezone
            lease.end_date = timezone.localdate()
        if commit:
            lease.save()
        return lease
