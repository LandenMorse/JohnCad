from django import forms
from django.db import transaction

from .models import AddPrinterTicket, Filament, Printer, PrinterFilament


class SerialNumberField(forms.CharField):
    """Serial numbers are printed in capitals; accept them typed either way,
    with or without spaces."""

    def to_python(self, value):
        return ''.join(super().to_python(value).split()).upper()


class AddPrinterTicketForm(forms.ModelForm):
    class Meta:
        model = AddPrinterTicket
        fields = ['name', 'printer_model', 'ip_address', 'access_code', 'serial_number']
        field_classes = {'serial_number': SerialNumberField}
        widgets = {
            # Shown as typed: teachers copy it off the printer's screen.
            'access_code': forms.TextInput(attrs={'autocomplete': 'off', 'spellcheck': 'false'}),
            'serial_number': forms.TextInput(attrs={'autocomplete': 'off', 'spellcheck': 'false'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model allows a blank code because closed tickets blank theirs.
        self.fields['access_code'].required = True
        self.fields['printer_model'].empty_label = 'Choose a model'

    def _open_tickets(self):
        return AddPrinterTicket.objects.filter(status__in=AddPrinterTicket.OPEN_STATUSES)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if Printer.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError('A printer with this name already exists.')
        if self._open_tickets().filter(name__iexact=name).exists():
            raise forms.ValidationError('A printer with this name is already being added.')
        return name

    def clean_serial_number(self):
        serial = self.cleaned_data['serial_number']
        if Printer.objects.filter(serial_number=serial).exists():
            raise forms.ValidationError('A printer with this serial number has already been added.')
        if self._open_tickets().filter(serial_number=serial).exists():
            raise forms.ValidationError('A printer with this serial number is already being added.')
        return serial


class PrinterFilamentsForm(forms.Form):
    """Which filament is loaded in each of a printer's slots."""

    def __init__(self, *args, printer, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer = printer
        loaded = {slot.slot: slot.filament_id for slot in printer.filament_slots.all()}
        for slot in range(1, Printer.MAX_FILAMENT_SLOTS + 1):
            self.fields[f'slot_{slot}'] = forms.ModelChoiceField(
                queryset=Filament.objects.all(),
                required=False,
                empty_label='Empty',
                label=f'Slot {slot}',
                initial=loaded.get(slot),
            )

    @transaction.atomic
    def save(self):
        self.printer.filament_slots.all().delete()
        PrinterFilament.objects.bulk_create([
            PrinterFilament(printer=self.printer, filament=filament, slot=slot)
            for slot in range(1, Printer.MAX_FILAMENT_SLOTS + 1)
            if (filament := self.cleaned_data.get(f'slot_{slot}')) is not None
        ])
