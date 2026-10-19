from django import forms

from properties.models import Service
from .models import FolioItem, ServiceRequest


class ServiceRequestForm(forms.ModelForm):
    class Meta:
        model = ServiceRequest
        fields = ("service", "quantity", "customer_note")
        widgets = {"customer_note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["quantity"].widget.attrs.update({"min": 1, "max": 100})
        self.fields["customer_note"].required = False

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity > 100:
            raise forms.ValidationError("Quantity cannot exceed 100.")
        return quantity


class StaffUsageForm(forms.ModelForm):
    class Meta:
        model = FolioItem
        fields = ("service", "quantity", "note")
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["quantity"].widget.attrs.update({"min": 1, "max": 100})

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity > 100:
            raise forms.ValidationError("Quantity cannot exceed 100.")
        return quantity
