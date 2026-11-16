from django import forms
from django.db import transaction

from .models import AddPrinterTicket, Printer, SlicerTemplate


class SerialNumberField(forms.CharField):
    """Serial numbers are printed in capitals; accept them typed either way,
    with or without spaces."""

    def to_python(self, value):
        return ''.join(super().to_python(value).split()).upper()


class AddPrinterTicketForm(forms.ModelForm):
    class Meta:
        model = AddPrinterTicket
        fields = ['name', 'ip_address', 'access_code', 'serial_number']
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


class PrinterForm(forms.ModelForm):
    """A teacher's settings for one printer: its name, whether it is in use,
    and which slicer templates can be chosen for it."""

    class Meta:
        model = Printer
        fields = ['name', 'is_active', 'templates', 'default_template']
        widgets = {'templates': forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['default_template'].empty_label = 'First one ticked'

    def clean(self):
        cleaned = super().clean()
        default = cleaned.get('default_template')
        if default is not None and default not in cleaned.get('templates', ()):
            self.add_error('default_template', 'The default must be one of the ticked templates.')
        return cleaned

    @transaction.atomic
    def save(self):
        # Not ModelForm.save(): set_templates keeps the default and the
        # templates consistent in one place.
        self.instance.save(update_fields=['name', 'is_active'])
        self.instance.set_templates(
            self.cleaned_data['templates'], self.cleaned_data['default_template'],
        )
        return self.instance


class SlicerTemplateForm(forms.ModelForm):
    """Upload a slicer template and say which printers it is for."""
    printers = forms.ModelMultipleChoiceField(
        queryset=Printer.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='Use on these printers',
        help_text='You can change this later on each printer.',
    )

    class Meta:
        model = SlicerTemplate
        fields = ['name', 'file']
        widgets = {'file': forms.FileInput(attrs={'accept': '.3mf'})}

    def __init__(self, *args, uploaded_by=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.uploaded_by = uploaded_by

    @transaction.atomic
    def save(self):
        template = super().save()
        for printer in self.cleaned_data['printers']:
            printer.offer_template(template)
        return template


class JobApprovalForm(forms.Form):
    """The teacher's choice when approving a job: one of a printer's
    templates, posted as `<printer id>:<template id>` so a single dropdown
    (see `approval_choices`) picks both."""
    UNAVAILABLE = 'That printer or template is no longer available.'

    target = forms.RegexField(
        r'^\d+:\d+$',
        error_messages={
            'required': 'Choose a printer and template.',
            'invalid': UNAVAILABLE,
        },
    )

    @staticmethod
    def approval_choices():
        """The printers a job can be approved for, each with its templates
        prefetched, for rendering the dropdown."""
        return Printer.objects.approvable().prefetch_related('templates')

    def clean_target(self):
        printer_pk, template_pk = self.cleaned_data['target'].split(':')
        printer = Printer.objects.approvable().filter(pk=printer_pk).first()
        template = printer and printer.templates.filter(pk=template_pk).first()
        if template is None:
            raise forms.ValidationError(self.UNAVAILABLE)
        self.cleaned_data['printer'] = printer
        self.cleaned_data['template'] = template
        return self.cleaned_data['target']
