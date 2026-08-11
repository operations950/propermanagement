from django import forms

from core.models import Property

from .models import Frequency, SessionTemplate


class SessionTemplateForm(forms.ModelForm):
    # JSONField's default form field is a raw-JSON Textarea — overridden
    # here with a real multi-select over Property.Type so "Restrict to
    # property types" behaves like every other property-type picker in the
    # app (see ticket_template_form.html's identical property_types field).
    property_types = forms.MultipleChoiceField(
        required=False, choices=Property.Type.choices, widget=forms.CheckboxSelectMultiple,
        help_text='Only used when Line source is "Property query". Empty = every active property type.',
    )

    # A plain one-line-per-item textarea rather than a dynamic add/remove
    # widget — this brief is functionality only (styling stays with the
    # existing design system), and a static line list changes rarely enough
    # that a textarea is the honest amount of UI for it. Replaces the
    # template's static_lines wholesale on save (see views.py) rather than
    # diffing, which is fine here: static lines carry no identity of their
    # own worth preserving across an edit (unlike a Session's already-
    # materialized SessionLines, which this never touches).
    static_lines_text = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 6}),
        label='Lines (one per line)',
        help_text='Only used when Line source is "Static list" — one line of text per session line, '
                   'e.g. one bank account name per row for a monthly bookkeeping session.',
    )

    class Meta:
        model = SessionTemplate
        fields = [
            'name', 'description', 'owner', 'department',
            'frequency', 'workday_of_month', 'next_open_date', 'due_offset_days',
            'active_from', 'active_until', 'is_active',
            'line_source', 'property_types', 'required_attributes', 'query_by_unit',
        ]
        widgets = {
            'required_attributes': forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['static_lines_text'].initial = '\n'.join(
                self.instance.static_lines.values_list('label', flat=True)
            )
            self.fields['property_types'].initial = self.instance.property_types

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('frequency') == Frequency.MONTHLY_WORKDAY and not cleaned.get('workday_of_month'):
            self.add_error('workday_of_month', 'Required for "Monthly (by working day)".')
        active_from = cleaned.get('active_from')
        active_until = cleaned.get('active_until')
        if active_from and active_until and active_from > active_until:
            self.add_error('active_until', 'Must be on or after "Active from".')
        return cleaned

    def save(self, commit=True):
        template = super().save(commit=commit)
        if commit:
            self._save_static_lines(template)
        return template

    def _save_static_lines(self, template):
        from .models import SessionTemplateLine
        template.static_lines.all().delete()
        labels = [line.strip() for line in self.cleaned_data.get('static_lines_text', '').split('\n') if line.strip()]
        SessionTemplateLine.objects.bulk_create([
            SessionTemplateLine(template=template, label=label, display_order=i)
            for i, label in enumerate(labels)
        ])
