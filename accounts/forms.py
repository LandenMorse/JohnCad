from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Classroom, SignupCode

User = get_user_model()

INVALID_CODE = 'This code is invalid or has expired.'


class CodeFieldMixin:
    """Validates a `code` field and keeps the matching SignupCode on the form."""

    signup_code = None

    def clean_code(self):
        self.signup_code = SignupCode.lookup(self.cleaned_data['code'])
        if self.signup_code is None:
            raise forms.ValidationError(INVALID_CODE)
        return self.signup_code.code


class SignupForm(CodeFieldMixin, UserCreationForm):
    code = forms.CharField(label='Class code', max_length=20)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('code', 'username', 'first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('first_name', 'last_name', 'email'):
            self.fields[name].required = True
        self.order_fields(['code', 'username', 'first_name', 'last_name', 'email'])


class JoinClassForm(CodeFieldMixin, forms.Form):
    code = forms.CharField(label='Class code', max_length=20)


class ClassroomForm(forms.ModelForm):
    class Meta:
        model = Classroom
        fields = ('name',)


class AddTeacherForm(forms.Form):
    username = forms.CharField(max_length=150)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        self.teacher = User.objects.filter(username=username, is_staff=True).first()
        if self.teacher is None:
            raise forms.ValidationError('No teacher (staff) account with that username.')
        return username
