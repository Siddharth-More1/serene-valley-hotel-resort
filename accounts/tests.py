from django.test import Client, TestCase
from django.urls import reverse

from .models import User


class AccountFlowTests(TestCase):
    def setUp(self):
        self.login_url = reverse("accounts:login")
        self.register_url = reverse("accounts:register")
        self.logout_url = reverse("accounts:logout")
        self.profile_url = reverse("accounts:profile")

    def registration_data(self, **overrides):
        data = {
            "username": "valleyguest",
            "first_name": "Valley",
            "last_name": "Guest",
            "email": "valley@example.com",
            "phone": "1234567890",
            "password1": "StrongPass!9284",
            "password2": "StrongPass!9284",
        }
        data.update(overrides)
        return data

    def test_registration_hashes_password_and_signs_in_customer(self):
        response = self.client.post(self.register_url, self.registration_data())

        user = User.objects.get(username="valleyguest")
        self.assertNotEqual(user.password, "StrongPass!9284")
        self.assertTrue(user.check_password("StrongPass!9284"))
        self.assertEqual(user.role, User.Role.CUSTOMER)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.profile_url)
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.assertContains(self.client.get(self.profile_url), "account has been created")

    def test_duplicate_username_and_email_are_rejected(self):
        User.objects.create_user(username="valleyguest", email="other@example.com")
        response = self.client.post(self.register_url, self.registration_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.filter(username="valleyguest").count(), 1)

        User.objects.create_user(username="otherguest", email="valley@example.com")
        response = self.client.post(
            self.register_url,
            self.registration_data(username="thirdguest"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "email address already exists")
        self.assertFalse(User.objects.filter(username="thirdguest").exists())

    def test_invalid_login_shows_generic_error(self):
        response = self.client.post(
            self.login_url,
            {"username": "missing", "password": "incorrect"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username or password")

    def test_login_redirects_to_local_next_url_and_rejects_external_next(self):
        user = User.objects.create_user(username="guest", password="CorrectPass!654")
        response = self.client.post(
            f"{self.login_url}?next=/bookings/my-bookings/",
            {"username": user.username, "password": "CorrectPass!654", "next": "/bookings/my-bookings/"},
        )
        self.assertRedirects(response, "/bookings/my-bookings/", fetch_redirect_response=False)

        self.client.logout()
        response = self.client.post(
            self.login_url,
            {"username": user.username, "password": "CorrectPass!654", "next": "https://bad.example/"},
        )
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_profile_requires_login(self):
        response = self.client.get(self.profile_url)
        self.assertRedirects(
            response,
            f"{self.login_url}?next={self.profile_url}",
            fetch_redirect_response=False,
        )

    def test_navbar_shows_logged_out_and_logged_in_options(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, self.login_url)
        self.assertContains(response, self.register_url)
        self.assertNotContains(response, "Log out")

        user = User.objects.create_user(
            username="guest",
            first_name="Serene Guest",
            password="CorrectPass!654",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Serene Guest")
        self.assertContains(response, reverse("bookings:my_bookings"))
        self.assertContains(response, self.logout_url)
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertNotContains(response, ">Register</a>")

    def test_logout_requires_post_and_clears_session(self):
        client = Client(enforce_csrf_checks=True)
        user = User.objects.create_user(username="guest", password="CorrectPass!654")
        client.force_login(user)

        self.assertEqual(client.get(self.logout_url).status_code, 405)
        client.get(reverse("home"))
        csrf_token = client.cookies["csrftoken"].value
        self.assertEqual(client.post(self.logout_url).status_code, 403)
        response = client.post(self.logout_url, HTTP_X_CSRFTOKEN=csrf_token)
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertContains(client.get(reverse("home")), "signed out")
