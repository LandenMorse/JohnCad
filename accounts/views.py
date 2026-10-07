from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import AddTeacherForm, ClassroomForm, JoinClassForm, SignupForm
from .models import Classroom, SignupCode
from .permissions import classes_visible_to, staff_required

User = get_user_model()


class CmsLoginView(LoginView):
    template_name = 'accounts/login.html'


def signup(request):
    if request.user.is_authenticated:
        return redirect('classroom-list')
    if request.method == 'POST':
        form = SignupForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                form.signup_code.classroom.students.add(user)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f'Welcome! You joined {form.signup_code.classroom.name}.')
            return redirect('attachment-list')
    else:
        form = SignupForm(initial={'code': request.GET.get('code', '')})
    return render(request, 'accounts/signup.html', {'form': form})


@login_required
def classroom_list(request):
    context = {'join_form': JoinClassForm()}
    if request.user.is_staff:
        context['taught'] = classes_visible_to(request.user).annotate(
            student_count=Count('students', distinct=True),
        ).order_by('name').prefetch_related('teachers')
        context['classroom_form'] = ClassroomForm()
    context['joined'] = request.user.classes.prefetch_related('teachers')
    return render(request, 'accounts/classroom_list.html', context)


@staff_required
@require_POST
def classroom_create(request):
    form = ClassroomForm(request.POST)
    if form.is_valid():
        classroom = form.save(commit=False)
        classroom.created_by = request.user
        classroom.save()
        classroom.teachers.add(request.user)
        messages.success(request, f'Class "{classroom.name}" created.')
        return redirect('classroom-detail', pk=classroom.pk)
    messages.error(request, 'Class name is required.')
    return redirect('classroom-list')


@login_required
@require_POST
def classroom_join(request):
    form = JoinClassForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors['code'][0])
        return redirect('classroom-list')
    classroom = form.signup_code.classroom
    if classroom.students.filter(pk=request.user.pk).exists():
        messages.info(request, f'You are already in {classroom.name}.')
    else:
        classroom.students.add(request.user)
        messages.success(request, f'You joined {classroom.name}.')
    return redirect('classroom-list')


def _managed_classroom(request, pk):
    classroom = get_object_or_404(Classroom, pk=pk)
    if not classroom.can_manage(request.user):
        raise Http404
    return classroom


@staff_required
def classroom_detail(request, pk):
    classroom = _managed_classroom(request, pk)
    active = classroom.active_codes().first()
    return render(request, 'accounts/classroom_detail.html', {
        'classroom': classroom,
        'students': classroom.students.order_by('last_name', 'first_name', 'username'),
        'teachers': classroom.teachers.order_by('username'),
        'codes': classroom.codes.all()[:20],
        'active_code': active,
        'share_url': (
            request.build_absolute_uri(f"{reverse('signup')}?code={active.code}")
            if active else ''
        ),
        'teacher_form': AddTeacherForm(),
    })


@staff_required
@require_POST
def code_create(request, pk):
    classroom = _managed_classroom(request, pk)
    code = SignupCode.generate(classroom, created_by=request.user)
    messages.success(request, f'New code {code.code} is valid for 24 hours.')
    return redirect('classroom-detail', pk=classroom.pk)


@staff_required
@require_POST
def code_revoke(request, pk, code_pk):
    classroom = _managed_classroom(request, pk)
    code = get_object_or_404(classroom.codes, pk=code_pk)
    if code.is_active:
        code.revoke()
        messages.success(request, f'Code {code.code} revoked.')
    return redirect('classroom-detail', pk=classroom.pk)


@staff_required
@require_POST
def student_remove(request, pk, user_pk):
    classroom = _managed_classroom(request, pk)
    student = get_object_or_404(classroom.students, pk=user_pk)
    classroom.students.remove(student)
    messages.success(request, f'{student.username} removed from {classroom.name}.')
    return redirect('classroom-detail', pk=classroom.pk)


@staff_required
@require_POST
def teacher_add(request, pk):
    classroom = _managed_classroom(request, pk)
    form = AddTeacherForm(request.POST)
    if form.is_valid():
        classroom.teachers.add(form.teacher)
        messages.success(request, f'{form.teacher.username} added as a teacher.')
    else:
        messages.error(request, form.errors['username'][0])
    return redirect('classroom-detail', pk=classroom.pk)


@staff_required
@require_POST
def teacher_remove(request, pk, user_pk):
    classroom = _managed_classroom(request, pk)
    teacher = get_object_or_404(classroom.teachers, pk=user_pk)
    if classroom.teachers.count() <= 1:
        messages.error(request, 'A class needs at least one teacher.')
        return redirect('classroom-detail', pk=classroom.pk)
    classroom.teachers.remove(teacher)
    messages.success(request, f'{teacher.username} removed as a teacher.')
    if not classroom.can_manage(request.user):
        return redirect('classroom-list')
    return redirect('classroom-detail', pk=classroom.pk)
