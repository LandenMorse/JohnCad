from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Attachment


class AttachmentForm(forms.ModelForm):
    class Meta:
        model = Attachment
        fields = ['name', 'file']


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']
