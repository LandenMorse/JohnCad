from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import SIGNUP_CODE_ALPHABET, SIGNUP_CODE_LENGTH, Classroom, SignupCode

User = get_user_model()

PASSWORD = 'pixel-printer-42'


class ClassTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user('teacher', password='pw', is_staff=True)
        cls.classroom = Classroom.objects.create(name='Period 1')
        cls.classroom.teachers.add(cls.teacher)

    def make_code(self, **kwargs):
        return SignupCode.generate(self.classroom, created_by=self.teacher, **kwargs)


class SignupCodeTests(ClassTestBase):
    def test_generated_code_shape_and_expiry(self):
        code = self.make_code()
        self.assertEqual(len(code.code), SIGNUP_CODE_LENGTH)
        self.assertTrue(set(code.code) <= set(SIGNUP_CODE_ALPHABET))
        self.assertAlmostEqual(
            code.expires_at, timezone.now() + timedelta(hours=24), delta=timedelta(minutes=1),
        )
        self.assertTrue(code.is_active)

    def test_lookup_normalizes_input(self):
        code = self.make_code()
        messy = f'  {code.code[:4].lower()}-{code.code[4:].lower()} '
        self.assertEqual(SignupCode.lookup(messy), code)

    def test_lookup_rejects_expired_revoked_and_unknown(self):
        expired = self.make_code()
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save()
        revoked = self.make_code()
        revoked.revoke()
        self.assertIsNone(SignupCode.lookup(expired.code))
        self.assertIsNone(SignupCode.lookup(revoked.code))
        self.assertIsNone(SignupCode.lookup('NOPE2345'))
        self.assertIsNone(SignupCode.lookup(''))


class SignupTests(ClassTestBase):
    def signup(self, code, username='newkid'):
        return self.client.post(reverse('signup'), {
            'code': code,
            'username': username,
            'first_name': 'New',
            'last_name': 'Kid',
            'email': f'{username}@example.com',
            'password1': PASSWORD,
            'password2': PASSWORD,
        })

    def test_valid_code_creates_student_in_class_and_logs_in(self):
        code = self.make_code()
        response = self.signup(code.code)
        self.assertRedirects(response, reverse('attachment-list'))
        user = User.objects.get(username='newkid')
        self.assertFalse(user.is_staff)
        self.assertEqual(list(user.classes.all()), [self.classroom])
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_code_is_multi_use(self):
        code = self.make_code()
        self.signup(code.code, 'kid1')
        self.client.logout()
        self.signup(code.code, 'kid2')
        self.assertEqual(self.classroom.students.count(), 2)

    def test_invalid_or_expired_code_rejected(self):
        code = self.make_code()
        code.revoke()
        for bad in (code.code, 'NOPE2345'):
            response = self.signup(bad)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'invalid or has expired')
        self.assertFalse(User.objects.filter(username='newkid').exists())

    def test_name_and_email_required(self):
        code = self.make_code()
        response = self.client.post(reverse('signup'), {
            'code': code.code, 'username': 'x', 'password1': PASSWORD, 'password2': PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        for field in ('first_name', 'last_name', 'email'):
            self.assertIn(field, response.context['form'].errors)

    def test_account_survives_code_expiry(self):
        code = self.make_code()
        self.signup(code.code)
        self.client.logout()
        code.expires_at = timezone.now() - timedelta(days=2)
        code.save()
        self.assertTrue(self.client.login(username='newkid', password=PASSWORD))

    def test_code_prefilled_from_query_string(self):
        response = self.client.get(reverse('signup'), {'code': 'ABCD2345'})
        self.assertContains(response, 'value="ABCD2345"')

    def test_logged_in_user_redirected(self):
        self.client.force_login(self.teacher)
        self.assertRedirects(self.client.get(reverse('signup')), reverse('classroom-list'))


class JoinClassTests(ClassTestBase):
    def setUp(self):
        self.student = User.objects.create_user('student', password='pw')
        self.client.force_login(self.student)

    def test_join_with_code(self):
        code = self.make_code()
        self.client.post(reverse('classroom-join'), {'code': code.code})
        self.assertIn(self.classroom, self.student.classes.all())

    def test_join_twice_is_harmless(self):
        code = self.make_code()
        self.client.post(reverse('classroom-join'), {'code': code.code})
        response = self.client.post(reverse('classroom-join'), {'code': code.code}, follow=True)
        self.assertContains(response, 'already in')
        self.assertEqual(self.classroom.students.count(), 1)

    def test_join_with_expired_code_fails(self):
        code = self.make_code()
        code.revoke()
        self.client.post(reverse('classroom-join'), {'code': code.code})
        self.assertFalse(self.student.classes.exists())


class ClassManagementTests(ClassTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_teacher = User.objects.create_user('other', password='pw', is_staff=True)
        cls.root = User.objects.create_superuser('root', password='pw')
        cls.student = User.objects.create_user('student', password='pw')

    def detail_status(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('classroom-detail', args=[self.classroom.pk])).status_code

    def test_detail_access(self):
        self.assertEqual(self.detail_status(self.teacher), 200)
        self.assertEqual(self.detail_status(self.root), 200)
        self.assertEqual(self.detail_status(self.other_teacher), 404)
        self.assertEqual(self.detail_status(self.student), 403)

    def test_co_teacher_can_manage(self):
        self.client.force_login(self.teacher)
        self.client.post(
            reverse('classroom-teacher-add', args=[self.classroom.pk]), {'username': 'other'},
        )
        self.assertEqual(self.detail_status(self.other_teacher), 200)

    def test_cannot_add_student_as_teacher(self):
        self.client.force_login(self.teacher)
        self.client.post(
            reverse('classroom-teacher-add', args=[self.classroom.pk]), {'username': 'student'},
        )
        self.assertFalse(self.classroom.teachers.filter(pk=self.student.pk).exists())

    def test_cannot_remove_last_teacher(self):
        self.client.force_login(self.teacher)
        self.client.post(
            reverse('classroom-teacher-remove', args=[self.classroom.pk, self.teacher.pk]),
        )
        self.assertTrue(self.classroom.teachers.filter(pk=self.teacher.pk).exists())

    def test_create_class_makes_creator_teacher(self):
        self.client.force_login(self.other_teacher)
        self.client.post(reverse('classroom-create'), {'name': 'Robotics'})
        classroom = Classroom.objects.get(name='Robotics')
        self.assertEqual(list(classroom.teachers.all()), [self.other_teacher])

    def test_generate_and_revoke_code(self):
        self.client.force_login(self.teacher)
        self.client.post(reverse('classroom-code-create', args=[self.classroom.pk]))
        code = self.classroom.codes.get()
        self.assertTrue(code.is_active)
        self.client.post(reverse('classroom-code-revoke', args=[self.classroom.pk, code.pk]))
        code.refresh_from_db()
        self.assertFalse(code.is_active)

    def test_other_teacher_cannot_generate_code(self):
        self.client.force_login(self.other_teacher)
        response = self.client.post(reverse('classroom-code-create', args=[self.classroom.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.classroom.codes.exists())

    def test_remove_student(self):
        self.classroom.students.add(self.student)
        self.client.force_login(self.teacher)
        self.client.post(
            reverse('classroom-student-remove', args=[self.classroom.pk, self.student.pk]),
        )
        self.assertFalse(self.classroom.students.exists())
        self.assertTrue(User.objects.filter(pk=self.student.pk).exists())

    def test_class_list_scoped(self):
        Classroom.objects.create(name='Hidden').teachers.add(self.other_teacher)
        self.client.force_login(self.teacher)
        names = [c.name for c in self.client.get(reverse('classroom-list')).context['taught']]
        self.assertEqual(names, ['Period 1'])
        self.client.force_login(self.root)
        names = [c.name for c in self.client.get(reverse('classroom-list')).context['taught']]
        self.assertEqual(names, ['Hidden', 'Period 1'])
