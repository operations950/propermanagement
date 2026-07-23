from django import forms

from .models import Property


class PropertyForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = ['name', 'property_type', 'address', 'timezone', 'is_general', 'is_active', 'notes']
        labels = {
            'name': 'Name',
            'property_type': 'Type',
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
