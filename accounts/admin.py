from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import Classroom, SignupCode

User = get_user_model()

# Re-register the default User admin so the admin panel shows Users too.
if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_by', 'created_at')
    search_fields = ('name',)
    filter_horizontal = ('teachers', 'students')


@admin.register(SignupCode)
class SignupCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'classroom', 'created_by', 'expires_at', 'is_active')
    list_filter = ('classroom',)
    search_fields = ('code',)

    @admin.display(boolean=True)
    def is_active(self, obj):
        return obj.is_active
