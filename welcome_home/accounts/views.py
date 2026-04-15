from datetime import timedelta
import random

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    AdminOTPForm,
    EmailVerificationForm,
    PasswordResetCodeForm,
    PasswordResetForm,
    PasswordResetRequestForm,
    ProfileUpdateForm,
    RegisterForm,
)
from .models import (
    AdminOTP,
    CustomUser,
    EmailVerificationOTP,
    LoginAttempt,
    PasswordResetOTP,
)
from .security import create_audit_log, get_client_ip
from chatapp.models import ChatRoom, Message
from rentals.models import Booking, Property, SavedProperty
from rentals.recommender import get_recommended_properties


def _generate_otp():
    return f"{random.randint(100000, 999999)}"


def _get_or_create_login_attempt(username, ip_address):
    attempt, _ = LoginAttempt.objects.get_or_create(
        username=username,
        ip_address=ip_address,
        defaults={'attempt_count': 0}
    )
    return attempt


def _clear_login_attempt(username, ip_address):
    LoginAttempt.objects.filter(username=username, ip_address=ip_address).delete()


def _send_email_verification_otp(user):
    code = _generate_otp()
    expires_at = timezone.now() + timedelta(
        seconds=getattr(settings, 'EMAIL_VERIFICATION_OTP_EXPIRY_SECONDS', 300)
    )

    EmailVerificationOTP.objects.filter(user=user, is_used=False).update(is_used=True)
    EmailVerificationOTP.objects.create(
        user=user,
        code=code,
        expires_at=expires_at
    )

    send_mail(
        subject='Welcome Home Email Verification Code',
        message=(
            f'Hello {user.username},\n\n'
            f'Your Welcome Home email verification code is: {code}\n'
            f'This code will expire in 5 minutes.\n\n'
            f'Please enter this code to activate your account.'
        ),
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@welcomehome.local'),
        recipient_list=[user.email],
        fail_silently=True,  # Prevents SMTP timeout in console backend
    )


def _send_password_reset_otp(user):
    code = _generate_otp()
    expires_at = timezone.now() + timedelta(
        seconds=getattr(settings, 'PASSWORD_RESET_OTP_EXPIRY_SECONDS', 300)
    )

    PasswordResetOTP.objects.filter(user=user, is_used=False).update(is_used=True)
    PasswordResetOTP.objects.create(
        user=user,
        code=code,
        expires_at=expires_at
    )

    send_mail(
        subject='Welcome Home Password Reset Code',
        message=(
            f'Hello {user.username},\n\n'
            f'Your password reset code is: {code}\n'
            f'This code will expire in 5 minutes.\n\n'
            f'Use this code to reset your password on the Welcome Home site.'
        ),
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@welcomehome.local'),
        recipient_list=[user.email],
        fail_silently=True,
    )
    print(f"🔑 PASSWORD RESET OTP for {user.email}: {code}")


# ========================
# AUTH
# ========================

def register_view(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.save()

            create_audit_log(
                request,
                action='REGISTER',
                user=user,
                details=f"New user registered with role {user.role}. Waiting for email verification."
            )

            _send_email_verification_otp(user)

            create_audit_log(
                request,
                action='EMAIL_VERIFICATION_SENT',
                user=user,
                details="Email verification code sent after registration."
            )

            request.session['pending_verification_user_id'] = user.id

            messages.success(
                request,
                "Registration successful. Please check your email for the verification code."
            )
            return redirect('verify_email')
    else:
        form = RegisterForm()

    return render(request, 'accounts/register.html', {'form': form})


def verify_email_view(request):
    user_id = request.session.get('pending_verification_user_id')
    if not user_id:
        messages.error(request, "Verification session expired. Please register again.")
        return redirect('register')

    user = get_object_or_404(CustomUser, pk=user_id)

    if request.method == 'POST':
        form = EmailVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['code']

            otp = EmailVerificationOTP.objects.filter(
                user=user,
                code=code,
                is_used=False
            ).order_by('-created_at').first()

            if not otp or not otp.is_valid():
                create_audit_log(
                    request,
                    action='EMAIL_VERIFICATION_FAILED',
                    user=user,
                    details="Invalid or expired registration verification code."
                )
                messages.error(request, "Invalid or expired verification code.")
            else:
                otp.is_used = True
                otp.save()

                user.is_active = True
                user.save()

                request.session.pop('pending_verification_user_id', None)

                create_audit_log(
                    request,
                    action='EMAIL_VERIFIED',
                    user=user,
                    details="User email verified successfully."
                )

                messages.success(request, "Your account has been verified. You can now log in.")
                return redirect('login')
    else:
        form = EmailVerificationForm()

    return render(request, 'accounts/verify_email.html', {
        'form': form,
        'pending_email': user.email,
    })


def resend_verification_otp_view(request):
    user_id = request.session.get('pending_verification_user_id')
    if not user_id:
        messages.error(request, "Verification session expired. Please register again.")
        return redirect('register')

    user = get_object_or_404(CustomUser, pk=user_id)

    if user.is_active:
        messages.info(request, "This account is already verified.")
        return redirect('login')

    _send_email_verification_otp(user)

    create_audit_log(
        request,
        action='EMAIL_VERIFICATION_SENT',
        user=user,
        details="Verification code resent."
    )

    messages.success(request, "A new verification code was sent to your email.")
    return redirect('verify_email')


def password_reset_request_view(request):
    if request.method == 'POST':
        form = PasswordResetRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            user = CustomUser.objects.filter(email__iexact=email, is_active=True).first()
            if user:
                _send_password_reset_otp(user)
                create_audit_log(
                    request,
                    action='PASSWORD_RESET_REQUESTED',
                    user=user,
                    details='Password reset code sent.'
                )
                request.session['pending_password_reset_user_id'] = user.id
                return redirect('password_reset_verify')

            messages.success(
                request,
                'If that email is registered, a password reset code has been sent.'
            )
            return redirect('password_reset')
    else:
        form = PasswordResetRequestForm()

    return render(request, 'accounts/password_reset_request.html', {'form': form})


def password_reset_verify_view(request):
    user_id = request.session.get('pending_password_reset_user_id')
    if not user_id:
        messages.error(request, "Password reset session expired. Please request a new code.")
        return redirect('password_reset')

    user = get_object_or_404(CustomUser, pk=user_id)

    if request.method == 'POST':
        form = PasswordResetCodeForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['code']
            otp = PasswordResetOTP.objects.filter(
                user=user,
                code=code,
                is_used=False
            ).order_by('-created_at').first()

            if not otp or not otp.is_valid():
                messages.error(request, "Invalid or expired reset code.")
            else:
                otp.is_used = True
                otp.save()

                request.session['password_reset_verified_user_id'] = user.id
                request.session.pop('pending_password_reset_user_id', None)

                messages.success(request, "Code verified. You can now set a new password.")
                return redirect('password_reset_confirm')
    else:
        form = PasswordResetCodeForm()

    return render(request, 'accounts/password_reset_verify.html', {
        'form': form,
        'pending_email': user.email,
    })


def resend_password_reset_otp_view(request):
    user_id = request.session.get('pending_password_reset_user_id')
    if not user_id:
        messages.error(request, "Password reset session expired. Please request a new code.")
        return redirect('password_reset')

    user = get_object_or_404(CustomUser, pk=user_id)

    _send_password_reset_otp(user)
    create_audit_log(
        request,
        action='PASSWORD_RESET_REQUESTED',
        user=user,
        details='Password reset code resent.'
    )

    messages.success(request, "A new password reset code was sent to your email.")
    return redirect('password_reset_verify')


def password_reset_confirm_view(request):
    user_id = request.session.get('password_reset_verified_user_id')
    if not user_id:
        messages.error(request, "Password reset session expired. Please verify your code again.")
        return redirect('password_reset')

    user = get_object_or_404(CustomUser, pk=user_id)

    if request.method == 'POST':
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            user.set_password(form.cleaned_data['password1'])
            user.save()
            request.session.pop('password_reset_verified_user_id', None)

            create_audit_log(
                request,
                action='PASSWORD_RESET_COMPLETED',
                user=user,
                details='User reset password successfully.'
            )

            messages.success(request, "Your password has been reset. Please log in with your new password.")
            return redirect('login')
    else:
        form = PasswordResetForm()

    return render(request, 'accounts/password_reset_confirm.html', {'form': form})


def login_view(request):
    form = AuthenticationForm(request, data=request.POST or None)
    ip_address = get_client_ip(request)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        attempt = _get_or_create_login_attempt(username, ip_address)

        if attempt.is_locked():
            messages.error(request, "Too many failed login attempts. Please try again later.")
            create_audit_log(
                request,
                action='LOGIN_FAILED',
                details=f"Locked account login attempt for username '{username}'."
            )
            return render(request, 'accounts/login.html', {'form': form})

        if form.is_valid():
            user = form.get_user()
            _clear_login_attempt(username, ip_address)

            if not user.is_active:
                request.session['pending_verification_user_id'] = user.id
                messages.error(request, "Your account is not verified yet. Please verify your email first.")
                return redirect('verify_email')

            if user.role == 'admin':
                if not user.email:
                    messages.error(request, "Admin account must have a valid email for MFA.")
                    return render(request, 'accounts/login.html', {'form': form})

                code = _generate_otp()
                expires_at = timezone.now() + timedelta(
                    seconds=getattr(settings, 'ADMIN_OTP_EXPIRY_SECONDS', 300)
                )

                AdminOTP.objects.filter(user=user, is_used=False).update(is_used=True)
                AdminOTP.objects.create(
                    user=user,
                    code=code,
                    expires_at=expires_at
                )

                request.session['pending_admin_user_id'] = user.id
                request.session['pending_admin_username'] = user.username

                send_mail(
                    subject='Welcome Home Admin Verification Code',
                    message=f'Your verification code is: {code}',
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@welcomehome.local'),
                    recipient_list=[user.email],
                    fail_silently=True,
                )

                create_audit_log(
                    request,
                    action='ADMIN_MFA_SENT',
                    user=user,
                    details="Admin MFA code sent."
                )

                messages.info(request, "A verification code was sent to the admin email.")
                otp_form = AdminOTPForm()
                return render(request, 'accounts/login.html', {
                    'form': form,
                    'otp_form': otp_form,
                    'mfa_required': True,
                    'pending_username': user.username,
                })

            login(request, user)
            create_audit_log(
                request,
                action='LOGIN_SUCCESS',
                user=user,
                details=f"User logged in with role {user.role}."
            )

            if user.role == 'admin':
                return redirect('dashboard_admin')
            elif user.role == 'landlord':
                return redirect('dashboard_landlord')
            else:
                return redirect('dashboard_client')

        else:
            attempt.attempt_count += 1
            if attempt.attempt_count >= getattr(settings, 'MAX_LOGIN_ATTEMPTS', 5):
                attempt.locked_until = timezone.now() + timedelta(
                    minutes=getattr(settings, 'LOGIN_LOCKOUT_MINUTES', 15)
                )
            attempt.save()

            create_audit_log(
                request,
                action='LOGIN_FAILED',
                details=f"Failed login attempt for username '{username}'."
            )
            messages.error(request, "Invalid username or password.")

    return render(request, 'accounts/login.html', {'form': form})


def verify_admin_otp_view(request):
    if request.method != 'POST':
        return redirect('login')

    user_id = request.session.get('pending_admin_user_id')
    if not user_id:
        messages.error(request, "Admin verification session expired.")
        return redirect('login')

    otp_form = AdminOTPForm(request.POST)
    auth_form = AuthenticationForm(request)

    if not otp_form.is_valid():
        return render(request, 'accounts/login.html', {
            'form': auth_form,
            'otp_form': otp_form,
            'mfa_required': True,
        })

    user = get_object_or_404(CustomUser, pk=user_id, role='admin')
    code = otp_form.cleaned_data['code']

    otp = AdminOTP.objects.filter(
        user=user,
        code=code,
        is_used=False
    ).order_by('-created_at').first()

    if not otp or not otp.is_valid():
        messages.error(request, "Invalid or expired verification code.")
        create_audit_log(
            request,
            action='LOGIN_FAILED',
            user=user,
            details="Invalid admin MFA code."
        )
        return render(request, 'accounts/login.html', {
            'form': auth_form,
            'otp_form': AdminOTPForm(),
            'mfa_required': True,
            'pending_username': user.username,
        })

    otp.is_used = True
    otp.save()

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session.pop('pending_admin_user_id', None)
    request.session.pop('pending_admin_username', None)

    create_audit_log(
        request,
        action='ADMIN_MFA_SUCCESS',
        user=user,
        details="Admin login completed with MFA."
    )
    create_audit_log(
        request,
        action='LOGIN_SUCCESS',
        user=user,
        details="Admin logged in successfully."
    )

    return redirect('dashboard_admin')


def logout_view(request):
    if request.user.is_authenticated:
        create_audit_log(
            request,
            action='LOGOUT',
            user=request.user,
            details="User logged out."
        )
    logout(request)
    return redirect('home')


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            create_audit_log(
                request,
                action='PROFILE_UPDATED',
                user=request.user,
                details="Profile updated."
            )
            messages.success(request, "Profile updated successfully.")
            return redirect('profile')
    else:
        form = ProfileUpdateForm(instance=request.user)

    return render(request, 'accounts/profile.html', {'form': form})


# ========================
# MAIN DASHBOARD ROUTER
# ========================

@login_required
def dashboard_view(request):
    if request.user.role == 'admin':
        return redirect('dashboard_admin')
    elif request.user.role == 'landlord':
        return redirect('dashboard_landlord')
    else:
        return redirect('dashboard_client')


# ========================
# ADMIN DASHBOARD
# ========================

@login_required
def admin_dashboard_view(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin dashboard."
        )
        return redirect('dashboard')

    context = {
        'total_users': CustomUser.objects.count(),
        'total_properties': Property.objects.count(),
        'total_bookings': Booking.objects.count(),
        'total_rooms': ChatRoom.objects.count(),
        'total_messages': Message.objects.count(),
    }
    return render(request, 'accounts/dashboard_admin.html', context)


@login_required
def admin_users(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin users."
        )
        return redirect('dashboard')

    users = CustomUser.objects.all().order_by('-date_joined')
    return render(request, 'accounts/admin_users.html', {'users': users})


@login_required
def admin_properties(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin properties."
        )
        return redirect('dashboard')

    properties = Property.objects.select_related('landlord').all().order_by('-created_at')
    return render(request, 'accounts/admin_properties.html', {'properties': properties})


@login_required
def admin_bookings(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin bookings."
        )
        return redirect('dashboard')

    bookings = Booking.objects.select_related('property', 'renter').all().order_by('-created_at')
    return render(request, 'accounts/admin_bookings.html', {'bookings': bookings})


@login_required
def admin_reports(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin reports."
        )
        return redirect('dashboard')

    total_users = CustomUser.objects.count()
    total_renters = CustomUser.objects.filter(role='renter').count()
    total_landlords = CustomUser.objects.filter(role='landlord').count()
    total_admins = CustomUser.objects.filter(role='admin').count()

    total_properties = Property.objects.count()
    available_properties = Property.objects.filter(is_available=True).count()
    unavailable_properties = Property.objects.filter(is_available=False).count()

    total_bookings = Booking.objects.count()
    pending_bookings = Booking.objects.filter(status='pending').count()
    accepted_bookings = Booking.objects.filter(status='accepted').count()
    rejected_bookings = Booking.objects.filter(status='rejected').count()
    rescheduled_bookings = Booking.objects.filter(status='rescheduled').count()

    total_rooms = ChatRoom.objects.count()
    total_messages = Message.objects.count()

    context = {
        'total_users': total_users,
        'total_renters': total_renters,
        'total_landlords': total_landlords,
        'total_admins': total_admins,
        'total_properties': total_properties,
        'available_properties': available_properties,
        'unavailable_properties': unavailable_properties,
        'total_bookings': total_bookings,
        'pending_bookings': pending_bookings,
        'accepted_bookings': accepted_bookings,
        'rejected_bookings': rejected_bookings,
        'rescheduled_bookings': rescheduled_bookings,
        'total_rooms': total_rooms,
        'total_messages': total_messages,
    }

    return render(request, 'accounts/admin_reports.html', context)


@login_required
def admin_settings(request):
    if request.user.role != 'admin':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-admin attempted to access admin settings."
        )
        return redirect('dashboard')

    return render(request, 'accounts/admin_settings.html')


# ========================
# LANDLORD DASHBOARD
# ========================

@login_required
def landlord_dashboard(request):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to access landlord dashboard."
        )
        return redirect('dashboard')

    my_properties = Property.objects.filter(landlord=request.user).order_by('-created_at')
    booking_requests = Booking.objects.filter(
        property__landlord=request.user
    ).select_related('property', 'renter').order_by('-created_at')

    context = {
        'my_properties': my_properties,
        'booking_requests': booking_requests,
        'total_properties': my_properties.count(),
        'total_bookings': booking_requests.count(),
        'pending_bookings': booking_requests.filter(status='pending').count(),
        'accepted_bookings': booking_requests.filter(status='accepted').count(),
    }

    return render(request, 'accounts/dashboard_landlord.html', context)


@login_required
def landlord_properties_view(request):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to access landlord properties."
        )
        return redirect('dashboard')

    properties = Property.objects.filter(landlord=request.user).order_by('-created_at')
    return render(request, 'accounts/landlord_properties.html', {'properties': properties})


@login_required
def landlord_messages(request):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to access landlord messages."
        )
        return redirect('dashboard')

    return render(request, 'accounts/landlord_messages.html')


@login_required
def landlord_settings(request):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to access landlord settings."
        )
        return redirect('dashboard')

    return render(request, 'accounts/landlord_settings.html')


@login_required
def landlord_tenants(request):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to access landlord tenants."
        )
        return redirect('dashboard')

    bookings = Booking.objects.filter(
        property__landlord=request.user,
        status='accepted'
    ).select_related('property', 'renter').order_by('-created_at')

    return render(request, 'accounts/landlord_tenants.html', {'bookings': bookings})


# ========================
# CLIENT DASHBOARD
# ========================

@login_required
def client_dashboard(request):
    if request.user.role != 'renter':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-renter attempted to access client dashboard."
        )
        return redirect('dashboard')

    my_bookings = Booking.objects.filter(
        renter=request.user
    ).select_related('property').order_by('-created_at')

    saved_count = SavedProperty.objects.filter(renter=request.user).count()
    recommended_properties = get_recommended_properties(request.user)

    context = {
        'recommendations': recommended_properties,
        'total_bookings': my_bookings.count(),
        'pending_bookings': my_bookings.filter(status='pending').count(),
        'accepted_bookings': my_bookings.filter(status='accepted').count(),
        'recent_bookings': my_bookings[:3],
        'saved_count': saved_count,
        'message_count': 0,
    }

    return render(request, 'accounts/dashboard_client.html', context)


@login_required
def renter_bookings_view(request):
    if request.user.role != 'renter':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-renter attempted to access renter bookings."
        )
        return redirect('dashboard')

    bookings = Booking.objects.filter(
        renter=request.user
    ).select_related('property').order_by('-created_at')

    return render(request, 'accounts/renter_bookings.html', {'bookings': bookings})


@login_required
def client_saved(request):
    if request.user.role != 'renter':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-renter attempted to access saved properties."
        )
        return redirect('dashboard')

    saved_properties = SavedProperty.objects.filter(
        renter=request.user
    ).select_related('property').order_by('-saved_at')

    return render(request, 'accounts/client_saved.html', {'saved_properties': saved_properties})


@login_required
def client_messages(request):
    if request.user.role != 'renter':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-renter attempted to access client messages."
        )
        return redirect('dashboard')

    return render(request, 'accounts/client_messages.html')


@login_required
def client_settings(request):
    if request.user.role != 'renter':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-renter attempted to access client settings."
        )
        return redirect('dashboard')

    return render(request, 'accounts/client_settings.html')


# ========================
# BOOKING ACTIONS
# ========================

@login_required
def booking_accept(request, pk):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to accept booking."
        )
        return redirect('dashboard')

    booking = get_object_or_404(Booking, pk=pk, property__landlord=request.user)
    booking.status = 'accepted'
    booking.save()

    create_audit_log(
        request,
        action='BOOKING_ACCEPTED',
        user=request.user,
        details=f"Booking {booking.pk} accepted for property {booking.property.title}."
    )
    return redirect('dashboard_landlord')


@login_required
def booking_reject(request, pk):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to reject booking."
        )
        return redirect('dashboard')

    booking = get_object_or_404(Booking, pk=pk, property__landlord=request.user)
    booking.status = 'rejected'
    booking.save()

    create_audit_log(
        request,
        action='BOOKING_REJECTED',
        user=request.user,
        details=f"Booking {booking.pk} rejected for property {booking.property.title}."
    )
    return redirect('dashboard_landlord')


@login_required
def booking_reschedule(request, pk):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to reschedule booking."
        )
        return redirect('dashboard')

    booking = get_object_or_404(Booking, pk=pk, property__landlord=request.user)
    booking.status = 'rescheduled'
    booking.save()

    create_audit_log(
        request,
        action='BOOKING_RESCHEDULED',
        user=request.user,
        details=f"Booking {booking.pk} rescheduled for property {booking.property.title}."
    )
    return redirect('dashboard_landlord')


@login_required
def booking_delete(request, pk):
    if request.user.role != 'landlord':
        create_audit_log(
            request,
            action='UNAUTHORIZED_ACCESS',
            details="Non-landlord attempted to delete booking."
        )
        return redirect('dashboard')

    booking = get_object_or_404(Booking, pk=pk, property__landlord=request.user)
    booking_id = booking.pk
    property_title = booking.property.title
    booking.delete()

    create_audit_log(
        request,
        action='BOOKING_DELETED',
        user=request.user,
        details=f"Booking {booking_id} deleted for property {property_title}."
    )
    return redirect('dashboard_landlord')