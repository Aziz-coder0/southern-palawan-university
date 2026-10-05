import csv
import hmac
import io
import os
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

import click
from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, make_response, redirect, render_template, send_file,
                   request, session, url_for)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from models import (Applicant, ApplicantProfile, ApplicantAcademicGrade, ApplicantAcademicReview,
                    ApplicantDocument, ApplicationRequirement, AuditLog, Employee,
                    EmployeeAttendance, Enrollment, FeeCharge, Grade, Notification, Payment,
                    Section, Student, StudentAttendance, Subject, User, LoginAttempt, db, utcnow)


ROLES = {"admin", "admissions", "academic", "finance", "hr", "teacher", "student"}
REQUIREMENTS = ("Application form", "Transcript of records", "Valid ID")
ATTENDANCE = {"present", "absent", "late", "excused"}
PERIODS = ("Prelim", "Midterm", "Pre-final", "Final")
ACADEMIC_DOCUMENT_LIMIT = 2 * 1024 * 1024


class InputError(Exception):
    pass


def required(name, limit=255):
    value = request.form.get(name, "").strip()
    if not value:
        raise InputError(f"{name.replace('_', ' ').title()} is required.")
    if len(value) > limit:
        raise InputError(f"{name.replace('_', ' ').title()} is too long.")
    return value


def optional(name, limit=255):
    value = request.form.get(name, "").strip()
    if len(value) > limit:
        raise InputError(f"{name.replace('_', ' ').title()} is too long.")
    return value


def email_address(name="email"):
    value = required(name, 255).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise InputError("Enter a valid email address.")
    return value


def phone_number(name):
    value = required(name, 40)
    if not re.fullmatch(r"\+?[0-9 ()-]{7,40}", value) or len(re.sub(r"\D", "", value)) < 7:
        raise InputError(f"Enter a valid {name.replace('_', ' ')}.")
    return value


def positive_int(name):
    try:
        value = int(required(name, 20))
    except ValueError:
        raise InputError(f"{name.replace('_', ' ').title()} must be a number.") from None
    if value < 1:
        raise InputError(f"{name.replace('_', ' ').title()} must be positive.")
    return value


def optional_id(name):
    raw = optional(name, 20)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise InputError(f"{name.replace('_', ' ').title()} must be a number.") from None
    if value < 1:
        raise InputError(f"{name.replace('_', ' ').title()} must be positive.")
    return value


def money(name):
    try:
        value = Decimal(required(name, 30))
    except InvalidOperation:
        raise InputError(f"{name.replace('_', ' ').title()} must be a valid amount.") from None
    if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
        raise InputError("Amount must be positive with at most two decimal places.")
    return value


def day(name, allow_blank=False):
    value = optional(name, 10) if allow_blank else required(name, 10)
    if not value and allow_blank:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError(f"{name.replace('_', ' ').title()} must be a valid date.") from None


def choice(name, allowed):
    value = required(name, 40)
    if value not in allowed:
        raise InputError(f"Invalid {name.replace('_', ' ')}.")
    return value


def applicant_profile_fields():
    birth_date = day("birth_date")
    if birth_date > date.today():
        raise InputError("Birth date cannot be in the future.")
    return {
        "applicant_type": choice("applicant_type", {"freshman", "transferee", "returnee"}),
        "middle_name": optional("middle_name", 80),
        "birth_date": birth_date,
        "home_address": required("home_address", 300),
        "previous_school": required("previous_school", 180),
        "last_school_year": optional("last_school_year", 20),
        "emergency_name": required("emergency_name", 160),
        "emergency_relationship": required("emergency_relationship", 60),
        "emergency_phone": phone_number("emergency_phone"),
    }


def academic_grades_from_form(applicant_type):
    entries = []
    for level in (8, 9, 10, 11):
        subjects = request.form.getlist(f"subject_{level}[]")
        values = request.form.getlist(f"grade_{level}[]")
        if len(subjects) != len(values):
            raise InputError(f"Grade {level} subject and grade rows do not match.")
        seen = set()
        for subject_raw, grade_raw in zip(subjects, values):
            subject, grade_raw = subject_raw.strip(), grade_raw.strip()
            if not subject and not grade_raw:
                continue
            if not subject or len(subject) > 120:
                raise InputError(f"Enter a subject name for Grade {level} (120 characters maximum).")
            if subject.casefold() in seen:
                raise InputError(f"Grade {level} contains the same subject twice.")
            seen.add(subject.casefold())
            try:
                grade = Decimal(grade_raw)
            except InvalidOperation:
                raise InputError(f"Enter a valid final grade for {subject}.") from None
            if not grade.is_finite() or grade < 0 or grade > 100 or grade.as_tuple().exponent < -2:
                raise InputError(f"Final grade for {subject} must be between 0 and 100 with up to two decimals.")
            entries.append(ApplicantAcademicGrade(grade_level=level, subject=subject, final_grade=grade))
        if applicant_type == "freshman" and not seen:
            raise InputError(f"Enter at least one final subject grade for Grade {level}.")
    if len(entries) > 80:
        raise InputError("Academic record has too many subject entries.")
    return entries


def academic_document_from_form(required_upload=False):
    upload = request.files.get("academic_document")
    if not upload or not upload.filename:
        if required_upload:
            raise InputError("Upload a PDF, JPG, or PNG copy of your academic record.")
        return None
    filename = secure_filename(upload.filename)[:180]
    data = upload.read(ACADEMIC_DOCUMENT_LIMIT + 1)
    if not data or len(data) > ACADEMIC_DOCUMENT_LIMIT:
        raise InputError("Academic record file must be between 1 byte and 2 MB.")
    suffix = os.path.splitext(filename)[1].lower()
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        content_type = "application/pdf"
    elif suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    elif suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    else:
        raise InputError("Academic record must be a valid PDF, JPG, or PNG file.")
    return ApplicantDocument(filename=filename, content_type=content_type,
                             data=data, size_bytes=len(data))


def set_academic_submission(applicant, *, require_document=False):
    applicant_type = applicant.profile.applicant_type
    grades = academic_grades_from_form(applicant_type)
    document = academic_document_from_form(required_upload=require_document or not applicant.academic_document)
    applicant.academic_grades = grades
    if document:
        applicant.academic_document = document
        for item in applicant.requirements:
            if item.name == "Transcript of records":
                item.received = True
                item.received_at = utcnow()
    if applicant.academic_review:
        applicant.academic_review.status = "pending"
        applicant.academic_review.note = ""
        applicant.academic_review.reviewer_id = None
        applicant.academic_review.reviewed_at = None
    else:
        applicant.academic_review = ApplicantAcademicReview(status="pending")


def audit(action, entity, entity_id=None, detail=""):
    db.session.add(AuditLog(user_id=g.user.id if g.user else None, action=action,
                            entity=entity, entity_id=entity_id, detail=detail[:300]))


def notify(title, message, link, *, roles=(), user_ids=(), kind="info"):
    recipients = set(user_ids)
    if roles:
        recipients.update(db.session.scalars(db.select(User.id).where(
            User.role.in_(roles), User.is_active.is_(True))).all())
    actor_id = g.user.id if g.user else None
    for user_id in recipients:
        if user_id and user_id != actor_id:
            db.session.add(Notification(user_id=user_id, title=title[:120],
                                        message=message[:260], link=link[:255], kind=kind))


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if roles and g.user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return decorator


def redirect_back(default="dashboard"):
    ref = request.referrer
    if ref and ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for(default))


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True,
                static_folder="public/static", static_url_path="/static")
    database_url = os.getenv("DATABASE_URL", "sqlite:///isms.db")
    if os.getenv("VERCEL") and not database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("Set DATABASE_URL to a persistent PostgreSQL database on Vercel.")
    if database_url.startswith("sqlite:"):
        os.makedirs(app.instance_path, exist_ok=True)
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key and os.getenv("VERCEL"):
        raise RuntimeError("Set SECRET_KEY in Vercel environment variables.")
    app.config.update(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300,
                                   "pool_size": 3, "max_overflow": 2} if database_url.startswith("postgresql") else {},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1" or bool(os.getenv("VERCEL")),
        APP_NAME=os.getenv("APP_NAME", "Southern Philippines University"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)

    @app.before_request
    def load_user_and_check_csrf():
        g.user = db.session.get(User, session.get("user_id")) if session.get("user_id") else None
        if g.user and not g.user.is_active:
            session.clear()
            g.user = None
        if g.user and g.user.employee and g.user.employee.status == "inactive":
            session.clear()
            g.user = None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            token = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            if not token or not hmac.compare_digest(token, supplied):
                abort(400, "Your form expired. Refresh the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        return response

    @app.context_processor
    def global_context():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        recent_notifications = []
        unread_notifications = 0
        if g.user:
            recent_notifications = db.session.scalars(db.select(Notification).where(
                Notification.user_id == g.user.id).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(6)).all()
            unread_notifications = db.session.scalar(db.select(func.count(Notification.id)).where(
                Notification.user_id == g.user.id, Notification.read_at.is_(None))) or 0
        return {"current_user": g.user, "csrf_token": session["csrf_token"],
                "app_name": app.config["APP_NAME"], "today": date.today(),
                "periods": PERIODS, "recent_notifications": recent_notifications,
                "unread_notifications": unread_notifications}

    @app.template_filter("peso")
    def peso(value):
        return f"₱{Decimal(value or 0):,.2f}"

    @app.errorhandler(InputError)
    def input_error(exc):
        db.session.rollback()
        flash(str(exc), "error")
        return redirect_back()

    @app.errorhandler(IntegrityError)
    def integrity_error(exc):
        db.session.rollback()
        flash("A record with those details already exists or is still in use.", "error")
        return redirect_back()

    @app.errorhandler(403)
    def forbidden(exc):
        return render_template("error.html", title="Access restricted", message="Your account cannot access this page."), 403

    @app.errorhandler(404)
    def missing(exc):
        return render_template("error.html", title="Page not found", message="The requested record or page could not be found."), 404

    @app.errorhandler(400)
    def bad_request(exc):
        return render_template("error.html", title="Request could not be completed", message=getattr(exc, "description", "Please try again.")), 400

    @app.errorhandler(413)
    def upload_too_large(exc):
        return render_template("error.html", title="File too large", message="The upload exceeds the 2 MB academic-record limit."), 413

    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        click.echo("Database schema is ready.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option()
    def create_admin_command(email, name, password):
        if len(password) < 12:
            raise click.ClickException("Password must have at least 12 characters.")
        db.create_all()
        normalized = email.strip().lower()
        if db.session.scalar(db.select(User).filter_by(email=normalized)):
            raise click.ClickException("That email already has an account.")
        db.session.add(User(email=normalized, full_name=name.strip(), role="admin",
                            password_hash=generate_password_hash(password)))
        db.session.commit()
        click.echo("Administrator account created.")

    @app.route("/health")
    def health():
        return {"status": "ok"}

    @app.route("/")
    def index():
        return redirect(url_for("dashboard" if g.user else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = email_address()
            password = required("password", 500)
            cutoff = utcnow() - timedelta(minutes=15)
            recent_failures = db.session.scalar(db.select(func.count(LoginAttempt.id)).where(
                LoginAttempt.email == email, LoginAttempt.attempted_at >= cutoff)) or 0
            if recent_failures >= 8:
                flash("Too many sign in attempts. Try again in 15 minutes.", "error")
                return render_template("login.html"), 429
            user = db.session.scalar(db.select(User).filter_by(email=email))
            if not user or not user.is_active or (user.employee and user.employee.status == "inactive") or not check_password_hash(user.password_hash, password):
                db.session.add(LoginAttempt(email=email))
                db.session.commit()
                flash("Email or password is incorrect.", "error")
            else:
                db.session.execute(db.delete(LoginAttempt).where(LoginAttempt.email == email))
                session.clear()
                session["user_id"] = user.id
                session["csrf_token"] = secrets.token_urlsafe(32)
                user.last_login_at = utcnow()
                audit("login", "user", user.id)
                db.session.commit()
                return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/logout")
    @role_required()
    def logout():
        audit("logout", "user", g.user.id)
        db.session.commit()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    @role_required()
    def account():
        if request.method == "POST":
            old = required("current_password", 500)
            new = required("new_password", 500)
            if not check_password_hash(g.user.password_hash, old):
                raise InputError("Current password is incorrect.")
            if len(new) < 12:
                raise InputError("New password must have at least 12 characters.")
            g.user.password_hash = generate_password_hash(new)
            audit("change_password", "user", g.user.id)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("account"))
        return render_template("account.html")

    @app.get("/notifications")
    @role_required()
    def notifications():
        items = db.session.scalars(db.select(Notification).where(Notification.user_id == g.user.id)
                                   .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(100)).all()
        return render_template("notifications.html", notifications=items)

    @app.post("/notifications/<int:notification_id>/open")
    @role_required()
    def open_notification(notification_id):
        item = db.get_or_404(Notification, notification_id)
        if item.user_id != g.user.id:
            abort(403)
        if item.read_at is None:
            item.read_at = utcnow()
            db.session.commit()
        destination = item.link if item.link.startswith("/") and not item.link.startswith("//") else url_for("dashboard")
        return redirect(destination)

    @app.post("/notifications/read-all")
    @role_required()
    def read_all_notifications():
        db.session.execute(db.update(Notification).where(
            Notification.user_id == g.user.id, Notification.read_at.is_(None)).values(read_at=utcnow()))
        db.session.commit()
        flash("All notifications marked as read.", "success")
        return redirect_back("notifications")

    @app.route("/dashboard")
    @role_required()
    def dashboard():
        if g.user.role == "student":
            student = g.user.student
            return render_template("student_portal.html", student=student,
                                   enrollments=student.enrollments if student else [],
                                   charges=student.charges if student else [],
                                   grades=student.grades if student else [])
        metrics = {
            "applicants": db.session.scalar(db.select(func.count(Applicant.id))) or 0,
            "students": db.session.scalar(db.select(func.count(Student.id))) or 0,
            "sections": db.session.scalar(db.select(func.count(Section.id))) or 0,
            "employees": db.session.scalar(db.select(func.count(Employee.id))) or 0,
            "pending": db.session.scalar(db.select(func.count(Applicant.id)).where(Applicant.status.in_(["submitted", "under_review"]))) or 0,
            "accepted": db.session.scalar(db.select(func.count(Applicant.id)).where(Applicant.status == "accepted")) or 0,
            "active_employees": db.session.scalar(db.select(func.count(Employee.id)).where(Employee.status == "active")) or 0,
            "staff_present_today": db.session.scalar(db.select(func.count(EmployeeAttendance.id)).where(
                EmployeeAttendance.on_date == date.today(), EmployeeAttendance.status == "present")) or 0,
        }
        if g.user.role == "teacher":
            teacher_id = g.user.employee.id if g.user.employee else -1
            metrics["sections"] = db.session.scalar(db.select(func.count(Section.id)).where(Section.teacher_id == teacher_id)) or 0
            metrics["students"] = db.session.scalar(db.select(func.count(func.distinct(Enrollment.student_id))).join(Section).where(
                Section.teacher_id == teacher_id, Enrollment.status == "enrolled")) or 0
        recent = db.session.scalars(db.select(Applicant).order_by(Applicant.created_at.desc()).limit(5)).all() if g.user.role in {"admin", "admissions"} else []
        recent_charges = db.session.scalars(db.select(FeeCharge).order_by(FeeCharge.created_at.desc()).limit(5)).all() if g.user.role == "finance" else []
        recent_employees = db.session.scalars(db.select(Employee).order_by(Employee.created_at.desc()).limit(5)).all() if g.user.role == "hr" else []
        recent_sections = []
        if g.user.role in {"academic", "teacher"}:
            query = db.select(Section)
            if g.user.role == "teacher":
                query = query.where(Section.teacher_id == (g.user.employee.id if g.user.employee else -1))
            query = query.order_by(Section.school_year.desc(), Section.id.desc()).limit(5)
            recent_sections = db.session.scalars(query).all()
        billed = collected = Decimal("0")
        if g.user.role == "finance":
            finance_charges = db.session.scalars(db.select(FeeCharge).where(FeeCharge.voided.is_(False))).all()
            billed = sum((item.amount for item in finance_charges), Decimal("0"))
            collected = sum((item.paid for item in finance_charges), Decimal("0"))
        return render_template("dashboard.html", metrics=metrics, recent=recent,
                               recent_charges=recent_charges, recent_employees=recent_employees,
                               recent_sections=recent_sections, billed=billed, collected=collected)

    @app.route("/apply", methods=["GET", "POST"])
    def apply():
        if request.method == "POST":
            if request.form.get("consent") != "yes":
                raise InputError("Please acknowledge the admission data notice.")
            applicant = Applicant(reference="APP-" + secrets.token_hex(6).upper(),
                                  first_name=required("first_name", 80), last_name=required("last_name", 80),
                                  email=email_address(), phone=phone_number("phone"),
                                  program=required("program", 120), notes=optional("notes", 2000))
            applicant.profile = ApplicantProfile(**applicant_profile_fields())
            applicant.requirements = [ApplicationRequirement(name=name, received=name == "Application form",
                                                              received_at=utcnow() if name == "Application form" else None)
                                      for name in REQUIREMENTS]
            set_academic_submission(applicant, require_document=True)
            db.session.add(applicant)
            db.session.flush()
            audit("submit", "applicant", applicant.id, applicant.reference)
            notify("New admission application", f"{applicant.full_name} applied for {applicant.program}.",
                   url_for("application_detail", applicant_id=applicant.id), roles={"admin", "admissions"})
            db.session.commit()
            return render_template("application_submitted.html", applicant=applicant)
        return render_template("apply.html")

    @app.route("/track", methods=["GET", "POST"])
    def track():
        applicant = db.session.get(Applicant, session.get("tracked_applicant_id")) if session.get("tracked_applicant_id") else None
        if request.method == "POST":
            session.pop("tracked_applicant_id", None)
            reference = required("reference", 24).upper()
            email = email_address()
            applicant = db.session.scalar(db.select(Applicant).filter_by(reference=reference, email=email))
            if not applicant:
                flash("No application matches that reference and email.", "error")
            else:
                session["tracked_applicant_id"] = applicant.id
        return render_template("track.html", applicant=applicant)

    @app.route("/academic-records", methods=["GET", "POST"])
    def applicant_academic_records():
        applicant = db.session.get(Applicant, session.get("tracked_applicant_id")) if session.get("tracked_applicant_id") else None
        if not applicant:
            return redirect(url_for("track"))
        if applicant.status == "accepted" or applicant.student:
            raise InputError("Accepted applications can no longer update academic records.")
        if not applicant.profile:
            raise InputError("Contact admissions to complete your application details first.")
        if request.method == "POST":
            set_academic_submission(applicant)
            audit("resubmit_academic", "applicant", applicant.id, applicant.reference)
            notify("Academic record resubmitted", f"{applicant.full_name} updated academic records.",
                   url_for("application_detail", applicant_id=applicant.id), roles={"admin", "admissions"})
            db.session.commit()
            flash("Academic records submitted for review.", "success")
            return redirect(url_for("applicant_academic_records"))
        return render_template("academic_records.html", applicant=applicant)

    @app.route("/applications")
    @role_required("admin", "admissions")
    def applications():
        search = request.args.get("q", "").strip()
        query = db.select(Applicant).order_by(Applicant.created_at.desc())
        if search:
            query = query.where(or_(Applicant.reference.ilike(f"%{search}%"), Applicant.first_name.ilike(f"%{search}%"), Applicant.last_name.ilike(f"%{search}%")))
        return render_template("applications.html", applicants=db.session.scalars(query).all(), search=search)

    @app.post("/applications")
    @role_required("admin", "admissions")
    def create_application():
        applicant = Applicant(reference="APP-" + secrets.token_hex(6).upper(),
                              first_name=required("first_name", 80), last_name=required("last_name", 80),
                              email=email_address(), phone=phone_number("phone"),
                              program=required("program", 120), notes=optional("notes", 2000))
        applicant.profile = ApplicantProfile(**applicant_profile_fields())
        applicant.requirements = [ApplicationRequirement(name=name, received=name == "Application form",
                                                          received_at=utcnow() if name == "Application form" else None)
                                  for name in REQUIREMENTS]
        set_academic_submission(applicant, require_document=True)
        db.session.add(applicant)
        db.session.flush()
        audit("create", "applicant", applicant.id)
        notify("New admission application", f"{applicant.full_name} was added to admissions.",
               url_for("application_detail", applicant_id=applicant.id), roles={"admin", "admissions"})
        db.session.commit()
        flash("Application created.", "success")
        return redirect(url_for("application_detail", applicant_id=applicant.id))

    @app.get("/applications/<int:applicant_id>")
    @role_required("admin", "admissions")
    def application_detail(applicant_id):
        return render_template("application_detail.html", applicant=db.get_or_404(Applicant, applicant_id))

    @app.post("/applications/<int:applicant_id>/update")
    @role_required("admin", "admissions")
    def update_application(applicant_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        if applicant.student:
            raise InputError("Edit the linked student record after conversion.")
        if applicant.status == "accepted" and request.form.get("academic_update") == "1":
            raise InputError("Reopen the admission decision before updating academic records.")
        applicant.first_name = required("first_name", 80)
        applicant.last_name = required("last_name", 80)
        applicant.email = email_address()
        applicant.phone = phone_number("phone")
        applicant.program = required("program", 120)
        applicant.notes = optional("notes", 2000)
        profile_values = applicant_profile_fields()
        if applicant.profile:
            for field, value in profile_values.items():
                setattr(applicant.profile, field, value)
        else:
            applicant.profile = ApplicantProfile(**profile_values)
        if request.form.get("academic_update") == "1":
            set_academic_submission(applicant)
        audit("update", "applicant", applicant.id)
        db.session.commit()
        flash("Application details saved.", "success")
        return redirect(url_for("application_detail", applicant_id=applicant.id))

    @app.post("/applications/<int:applicant_id>/status")
    @role_required("admin", "admissions")
    def application_status(applicant_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        status = choice("status", {"submitted", "under_review", "accepted", "rejected"})
        if status == "accepted" and not all(r.received for r in applicant.requirements):
            raise InputError("Mark all requirements received before accepting the application.")
        if status == "accepted" and (not applicant.academic_review or applicant.academic_review.status != "verified"):
            raise InputError("Verify the academic record before accepting the application.")
        if applicant.student and status != "accepted":
            raise InputError("An enrolled student cannot be moved out of accepted status.")
        applicant.status = status
        audit("change_status", "applicant", applicant.id, status)
        if status in {"accepted", "rejected"}:
            notify("Admission decision recorded", f"{applicant.full_name}: {status}.",
                   url_for("application_detail", applicant_id=applicant.id), roles={"admin"})
        db.session.commit()
        flash("Application status updated.", "success")
        return redirect(url_for("application_detail", applicant_id=applicant.id))

    @app.post("/applications/<int:applicant_id>/academic-review")
    @role_required("admin", "admissions")
    def academic_review(applicant_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        if applicant.status == "accepted" or applicant.student:
            raise InputError("Reopen the admission decision before changing the academic review.")
        status = choice("status", {"pending", "verified", "needs_correction"})
        if status == "verified":
            if not applicant.academic_document:
                raise InputError("An academic record document is required before verification.")
            if applicant.profile and applicant.profile.applicant_type == "freshman":
                levels = {grade.grade_level for grade in applicant.academic_grades}
                if levels != {8, 9, 10, 11}:
                    raise InputError("First-year applicants need subject grades for Grades 8 to 11.")
        note = optional("note", 500)
        if status == "needs_correction" and not note:
            raise InputError("Explain what the applicant should correct.")
        review = applicant.academic_review or ApplicantAcademicReview()
        review.status = status
        review.note = note
        review.reviewer_id = g.user.id
        review.reviewed_at = utcnow()
        applicant.academic_review = review
        audit("academic_review", "applicant", applicant.id, status)
        db.session.commit()
        flash("Academic record review updated.", "success")
        return redirect(url_for("application_detail", applicant_id=applicant.id))

    @app.get("/applications/<int:applicant_id>/academic-document")
    @role_required("admin", "admissions")
    def download_academic_document(applicant_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        document = applicant.academic_document
        if not document:
            abort(404)
        response = send_file(io.BytesIO(document.data), mimetype=document.content_type,
                             as_attachment=True, download_name=document.filename, max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/applications/<int:applicant_id>/requirements/<int:requirement_id>")
    @role_required("admin", "admissions")
    def requirement_status(applicant_id, requirement_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        item = db.get_or_404(ApplicationRequirement, requirement_id)
        if item.applicant_id != applicant.id:
            abort(404)
        item.received = not item.received
        item.received_at = utcnow() if item.received else None
        if applicant.status == "accepted" and not item.received:
            raise InputError("Accepted applications must keep all requirements received.")
        audit("requirement", "applicant", applicant.id, f"{item.name}: {item.received}")
        db.session.commit()
        flash("Requirement updated.", "success")
        return redirect(url_for("application_detail", applicant_id=applicant.id))

    @app.post("/applications/<int:applicant_id>/convert")
    @role_required("admin", "admissions")
    def convert_application(applicant_id):
        applicant = db.get_or_404(Applicant, applicant_id)
        if applicant.status != "accepted" or applicant.student:
            raise InputError("Only an accepted application without a student record can be converted.")
        student = Student(student_number=required("student_number", 32), applicant_id=applicant.id,
                          first_name=applicant.first_name, last_name=applicant.last_name,
                          email=applicant.email, phone=applicant.phone, program=applicant.program,
                          year_level="1")
        db.session.add(student)
        db.session.flush()
        audit("convert", "student", student.id, f"From {applicant.reference}")
        notify("Student record ready", f"{student.full_name} is ready for enrollment and billing.",
               url_for("student_detail", student_id=student.id), roles={"academic", "finance"})
        db.session.commit()
        flash("Student record created from the accepted application.", "success")
        return redirect(url_for("student_detail", student_id=student.id))

    @app.get("/students")
    @role_required("admin", "admissions", "academic", "finance", "teacher")
    def students():
        search = request.args.get("q", "").strip()
        query = db.select(Student).order_by(Student.last_name, Student.first_name)
        if g.user.role == "teacher":
            assigned = db.select(Enrollment.student_id).join(Section).where(
                Section.teacher_id == (g.user.employee.id if g.user.employee else -1),
                Enrollment.status == "enrolled")
            query = query.where(Student.id.in_(assigned))
        if search:
            query = query.where(or_(Student.student_number.ilike(f"%{search}%"), Student.first_name.ilike(f"%{search}%"), Student.last_name.ilike(f"%{search}%")))
        return render_template("students.html", students=db.session.scalars(query).all(), search=search)

    @app.post("/students")
    @role_required("admin", "academic")
    def create_student():
        student = Student(student_number=required("student_number", 32), first_name=required("first_name", 80),
                          last_name=required("last_name", 80), email=email_address(),
                          phone=optional("phone", 40), program=required("program", 120),
                          year_level=required("year_level", 20))
        db.session.add(student)
        db.session.flush()
        audit("create", "student", student.id)
        db.session.commit()
        flash("Student added.", "success")
        return redirect(url_for("student_detail", student_id=student.id))

    @app.get("/students/<int:student_id>")
    @role_required("admin", "admissions", "academic", "finance", "teacher", "student")
    def student_detail(student_id):
        student = db.get_or_404(Student, student_id)
        if g.user.role == "student" and student.user_id != g.user.id:
            abort(403)
        if g.user.role == "teacher" and not db.session.scalar(db.select(Enrollment.id).join(Section).where(Enrollment.student_id == student.id, Section.teacher_id == g.user.employee.id if g.user.employee else -1, Enrollment.status == "enrolled")):
            abort(403)
        sections = db.session.scalars(db.select(Section).order_by(Section.school_year.desc(), Section.code)).all() if g.user.role in {"admin", "academic"} else []
        visible_enrollments = student.enrollments
        visible_grades = student.grades
        if g.user.role == "teacher":
            teacher_id = g.user.employee.id
            visible_enrollments = [e for e in student.enrollments if e.section.teacher_id == teacher_id]
            visible_grades = [grade for grade in student.grades if grade.section.teacher_id == teacher_id]
        return render_template("student_detail.html", student=student, sections=sections,
                               visible_enrollments=visible_enrollments, visible_grades=visible_grades)

    @app.post("/students/<int:student_id>/update")
    @role_required("admin", "academic")
    def update_student(student_id):
        student = db.get_or_404(Student, student_id)
        student.first_name = required("first_name", 80)
        student.last_name = required("last_name", 80)
        student.email = email_address()
        student.phone = optional("phone", 40)
        student.program = required("program", 120)
        student.year_level = required("year_level", 20)
        student.status = choice("status", {"active", "inactive", "graduated"})
        audit("update", "student", student.id)
        db.session.commit()
        flash("Student details saved.", "success")
        return redirect(url_for("student_detail", student_id=student.id))

    @app.post("/students/<int:student_id>/enroll")
    @role_required("admin", "academic")
    def enroll_student(student_id):
        student = db.session.scalar(db.select(Student).where(Student.id == student_id).with_for_update())
        if not student:
            abort(404)
        section = db.session.scalar(db.select(Section).where(Section.id == positive_int("section_id")).with_for_update())
        if not section:
            abort(404)
        if student.status != "active":
            raise InputError("Only active students can enroll.")
        enrolled_count = db.session.scalar(db.select(func.count(Enrollment.id)).where(Enrollment.section_id == section.id, Enrollment.status == "enrolled")) or 0
        existing = db.session.scalar(db.select(Enrollment).filter_by(student_id=student.id, section_id=section.id))
        if existing and existing.status == "enrolled":
            raise InputError("This student is already enrolled in the section.")
        if enrolled_count >= section.capacity:
            raise InputError("This section is full.")
        if db.session.scalar(db.select(Enrollment.id).join(Section).where(Enrollment.student_id == student.id, Enrollment.status == "enrolled", Section.subject_id == section.subject_id, Section.school_year == section.school_year, Section.term == section.term)):
            raise InputError("Student is already enrolled in this subject and term.")
        if existing:
            existing.status = "enrolled"
            existing.enrolled_at = utcnow()
        else:
            db.session.add(Enrollment(student_id=student.id, section_id=section.id))
        audit("enroll", "student", student.id, section.code)
        notify("Section enrollment", f"You were enrolled in {section.subject.code} · {section.code}.",
               url_for("dashboard"), user_ids=[student.user_id] if student.user_id else [])
        db.session.commit()
        flash("Student enrolled.", "success")
        return redirect(url_for("student_detail", student_id=student.id))

    @app.post("/enrollments/<int:enrollment_id>/drop")
    @role_required("admin", "academic")
    def drop_enrollment(enrollment_id):
        enrollment = db.get_or_404(Enrollment, enrollment_id)
        enrollment.status = "dropped"
        audit("drop", "enrollment", enrollment.id)
        db.session.commit()
        flash("Enrollment dropped.", "success")
        return redirect_back("students")

    @app.get("/subjects")
    @role_required("admin", "academic", "teacher")
    def subjects():
        return render_template("subjects.html", subjects=db.session.scalars(db.select(Subject).order_by(Subject.code)).all())

    @app.post("/subjects")
    @role_required("admin", "academic")
    def create_subject():
        subject = Subject(code=required("code", 32).upper(), title=required("title", 160),
                          units=positive_int("units"), description=optional("description", 2000))
        db.session.add(subject)
        db.session.flush()
        audit("create", "subject", subject.id)
        db.session.commit()
        flash("Subject added.", "success")
        return redirect(url_for("subjects"))

    @app.post("/subjects/<int:subject_id>/update")
    @role_required("admin", "academic")
    def update_subject(subject_id):
        subject = db.get_or_404(Subject, subject_id)
        subject.code = required("code", 32).upper()
        subject.title = required("title", 160)
        subject.units = positive_int("units")
        subject.description = optional("description", 2000)
        audit("update", "subject", subject.id)
        db.session.commit()
        flash("Subject updated.", "success")
        return redirect(url_for("subjects"))

    @app.get("/sections")
    @role_required("admin", "academic", "teacher")
    def sections():
        query = db.select(Section).order_by(Section.school_year.desc(), Section.term, Section.code)
        if g.user.role == "teacher":
            query = query.where(Section.teacher_id == (g.user.employee.id if g.user.employee else -1))
        return render_template("sections.html", sections=db.session.scalars(query).all(),
                               subjects=db.session.scalars(db.select(Subject).order_by(Subject.code)).all(),
                               teachers=db.session.scalars(db.select(Employee).where(Employee.status == "active").order_by(Employee.last_name)).all())

    @app.post("/sections")
    @role_required("admin", "academic")
    def create_section():
        subject = db.get_or_404(Subject, positive_int("subject_id"))
        teacher_id = optional_id("teacher_id")
        teacher = db.get_or_404(Employee, teacher_id) if teacher_id else None
        if teacher and teacher.status != "active":
            raise InputError("Teacher must be active.")
        section = Section(code=required("code", 32).upper(), subject_id=subject.id,
                          teacher_id=teacher.id if teacher else None,
                          school_year=required("school_year", 20), term=required("term", 30),
                          schedule=required("schedule", 120), room=optional("room", 60),
                          capacity=positive_int("capacity"))
        db.session.add(section)
        db.session.flush()
        audit("create", "section", section.id)
        db.session.commit()
        flash("Section created.", "success")
        return redirect(url_for("section_detail", section_id=section.id))

    def allowed_section(section):
        if g.user.role == "teacher" and section.teacher_id != (g.user.employee.id if g.user.employee else -1):
            abort(403)

    @app.get("/sections/<int:section_id>")
    @role_required("admin", "academic", "teacher")
    def section_detail(section_id):
        section = db.get_or_404(Section, section_id)
        allowed_section(section)
        roster = db.session.scalars(db.select(Enrollment).where(Enrollment.section_id == section.id, Enrollment.status == "enrolled").join(Student).order_by(Student.last_name)).all()
        selected_date = request.args.get("date", date.today().isoformat())
        try:
            selected_date = date.fromisoformat(selected_date)
        except ValueError:
            selected_date = date.today()
        attendance = {a.student_id: a for a in db.session.scalars(db.select(StudentAttendance).where(StudentAttendance.section_id == section.id, StudentAttendance.on_date == selected_date)).all()}
        grades = {(grade.student_id, grade.period): grade for grade in db.session.scalars(db.select(Grade).where(Grade.section_id == section.id)).all()}
        grade_values = {e.student_id: {period: str(grades[(e.student_id, period)].score)
                                       for period in PERIODS if (e.student_id, period) in grades} for e in roster}
        return render_template("section_detail.html", section=section, roster=roster,
                               selected_date=selected_date, attendance=attendance, grades=grades,
                               grade_values=grade_values,
                               teachers=db.session.scalars(db.select(Employee).where(Employee.status == "active").order_by(Employee.last_name)).all())

    @app.post("/sections/<int:section_id>/update")
    @role_required("admin", "academic")
    def update_section(section_id):
        section = db.get_or_404(Section, section_id)
        teacher_id = optional_id("teacher_id")
        teacher = db.get_or_404(Employee, teacher_id) if teacher_id else None
        section.teacher_id = teacher.id if teacher else None
        section.schedule = required("schedule", 120)
        section.room = optional("room", 60)
        section.capacity = positive_int("capacity")
        if section.capacity < sum(1 for e in section.enrollments if e.status == "enrolled"):
            raise InputError("Capacity cannot be lower than current enrollment.")
        audit("update", "section", section.id)
        db.session.commit()
        flash("Section updated.", "success")
        return redirect(url_for("section_detail", section_id=section.id))

    @app.post("/sections/<int:section_id>/attendance")
    @role_required("admin", "academic", "teacher")
    def save_attendance(section_id):
        section = db.get_or_404(Section, section_id)
        allowed_section(section)
        on_date = day("on_date")
        roster = [e.student_id for e in section.enrollments if e.status == "enrolled"]
        for student_id in roster:
            status = request.form.get(f"status_{student_id}", "")
            if status not in ATTENDANCE:
                raise InputError("Choose an attendance status for every student.")
            row = db.session.scalar(db.select(StudentAttendance).filter_by(section_id=section.id, student_id=student_id, on_date=on_date))
            if row:
                row.status = status
            else:
                db.session.add(StudentAttendance(section_id=section.id, student_id=student_id, on_date=on_date, status=status))
        audit("attendance", "section", section.id, on_date.isoformat())
        db.session.commit()
        flash("Attendance saved.", "success")
        return redirect(url_for("section_detail", section_id=section.id, date=on_date.isoformat()))

    @app.post("/sections/<int:section_id>/grades")
    @role_required("admin", "academic", "teacher")
    def save_grades(section_id):
        section = db.get_or_404(Section, section_id)
        allowed_section(section)
        period = choice("period", PERIODS)
        roster = [e.student_id for e in section.enrollments if e.status == "enrolled"]
        changed_student_ids = []
        for student_id in roster:
            raw = request.form.get(f"score_{student_id}", "").strip()
            if not raw:
                continue
            try:
                score = Decimal(raw)
            except InvalidOperation:
                raise InputError("Grades must be numeric.") from None
            if not score.is_finite() or score < 0 or score > 100 or score.as_tuple().exponent < -2:
                raise InputError("Grades must be between 0 and 100 with at most two decimal places.")
            row = db.session.scalar(db.select(Grade).filter_by(section_id=section.id, student_id=student_id, period=period))
            if row:
                if row.score != score:
                    changed_student_ids.append(student_id)
                row.score = score
            else:
                db.session.add(Grade(section_id=section.id, student_id=student_id, period=period, score=score))
                changed_student_ids.append(student_id)
        audit("grades", "section", section.id, period)
        for student_id in changed_student_ids:
            student = db.session.get(Student, student_id)
            if student.user_id:
                notify("Grade posted", f"A {period} grade was posted for {section.subject.code}.",
                       url_for("dashboard"), user_ids=[student.user_id])
        db.session.commit()
        flash("Grades saved.", "success")
        return redirect(url_for("section_detail", section_id=section.id))

    @app.get("/finance")
    @role_required("admin", "finance")
    def finance():
        charges = db.session.scalars(db.select(FeeCharge).order_by(FeeCharge.created_at.desc())).all()
        payments = db.session.scalars(db.select(Payment).order_by(Payment.created_at.desc()).limit(15)).all()
        students_list = db.session.scalars(db.select(Student).order_by(Student.last_name)).all()
        total_billed = sum((c.amount for c in charges if not c.voided), Decimal("0"))
        total_paid = sum((c.paid for c in charges if not c.voided), Decimal("0"))
        return render_template("finance.html", charges=charges, payments=payments, students=students_list,
                               total_billed=total_billed, total_paid=total_paid)

    @app.post("/finance/charges")
    @role_required("admin", "finance")
    def create_charge():
        student = db.get_or_404(Student, positive_int("student_id"))
        charge = FeeCharge(student_id=student.id, description=required("description", 160),
                           amount=money("amount"), due_on=day("due_on", allow_blank=True))
        db.session.add(charge)
        db.session.flush()
        audit("create", "charge", charge.id)
        if student.user_id:
            notify("New account charge", f"{charge.description}: {charge.amount:,.2f} PHP was added to your account.",
                   url_for("dashboard"), user_ids=[student.user_id])
        db.session.commit()
        flash("Charge added to student account.", "success")
        return redirect(url_for("finance"))

    @app.post("/finance/charges/<int:charge_id>/void")
    @role_required("admin", "finance")
    def void_charge(charge_id):
        charge = db.session.scalar(db.select(FeeCharge).where(FeeCharge.id == charge_id).with_for_update())
        if not charge:
            abort(404)
        if charge.voided:
            raise InputError("Charge is already voided.")
        if charge.paid > 0:
            raise InputError("Void its payments before voiding the charge.")
        charge.voided = True
        audit("void", "charge", charge.id)
        db.session.commit()
        flash("Charge voided.", "success")
        return redirect(url_for("finance"))

    @app.post("/finance/payments")
    @role_required("admin", "finance")
    def create_payment():
        charge = db.session.scalar(db.select(FeeCharge).where(FeeCharge.id == positive_int("charge_id")).with_for_update())
        if not charge:
            abort(404)
        if charge.voided:
            raise InputError("This charge is voided.")
        amount = money("amount")
        if amount > charge.balance:
            raise InputError("Payment cannot exceed the remaining balance.")
        payment = Payment(charge_id=charge.id, amount=amount, method=choice("method", {"cash", "bank", "online", "other"}),
                          reference=required("reference", 80), paid_on=day("paid_on"))
        db.session.add(payment)
        db.session.flush()
        audit("payment", "payment", payment.id, f"Charge {charge.id}")
        if charge.student.user_id:
            notify("Payment recorded", f"Your {payment.amount:,.2f} PHP payment was recorded.",
                   url_for("dashboard"), user_ids=[charge.student.user_id], kind="success")
        db.session.commit()
        flash("Payment recorded.", "success")
        return redirect(url_for("finance"))

    @app.post("/finance/payments/<int:payment_id>/void")
    @role_required("admin", "finance")
    def void_payment(payment_id):
        payment = db.get_or_404(Payment, payment_id)
        if payment.voided:
            raise InputError("Payment is already voided.")
        payment.voided = True
        audit("void", "payment", payment.id)
        if payment.charge.student.user_id:
            notify("Payment voided", f"Payment {payment.reference} was voided. Please review your balance.",
                   url_for("dashboard"), user_ids=[payment.charge.student.user_id], kind="warning")
        db.session.commit()
        flash("Payment voided. The balance has been restored.", "success")
        return redirect(url_for("finance"))

    @app.get("/employees")
    @role_required("admin", "hr", "academic")
    def employees():
        search = request.args.get("q", "").strip()
        query = db.select(Employee).order_by(Employee.last_name, Employee.first_name)
        if search:
            query = query.where(or_(Employee.employee_number.ilike(f"%{search}%"), Employee.first_name.ilike(f"%{search}%"), Employee.last_name.ilike(f"%{search}%")))
        return render_template("employees.html", employees=db.session.scalars(query).all(), search=search)

    @app.post("/employees")
    @role_required("admin", "hr")
    def create_employee():
        employee = Employee(employee_number=required("employee_number", 32), first_name=required("first_name", 80),
                            last_name=required("last_name", 80), email=email_address(),
                            phone=optional("phone", 40), department=required("department", 100),
                            position=required("position", 100), hired_on=day("hired_on", allow_blank=True))
        db.session.add(employee)
        db.session.flush()
        audit("create", "employee", employee.id)
        db.session.commit()
        flash("Employee added.", "success")
        return redirect(url_for("employee_detail", employee_id=employee.id))

    @app.get("/employees/<int:employee_id>")
    @role_required("admin", "hr", "academic")
    def employee_detail(employee_id):
        employee = db.get_or_404(Employee, employee_id)
        attendance = db.session.scalars(db.select(EmployeeAttendance).where(EmployeeAttendance.employee_id == employee.id).order_by(EmployeeAttendance.on_date.desc()).limit(15)).all() if g.user.role in {"admin", "hr"} else []
        return render_template("employee_detail.html", employee=employee, attendance=attendance)

    @app.post("/employees/<int:employee_id>/update")
    @role_required("admin", "hr")
    def update_employee(employee_id):
        employee = db.get_or_404(Employee, employee_id)
        employee.first_name = required("first_name", 80)
        employee.last_name = required("last_name", 80)
        employee.email = email_address()
        employee.phone = optional("phone", 40)
        employee.department = required("department", 100)
        employee.position = required("position", 100)
        employee.status = choice("status", {"active", "inactive", "on_leave"})
        employee.hired_on = day("hired_on", allow_blank=True)
        audit("update", "employee", employee.id)
        db.session.commit()
        flash("Employee details saved.", "success")
        return redirect(url_for("employee_detail", employee_id=employee.id))

    @app.post("/employees/<int:employee_id>/attendance")
    @role_required("admin", "hr")
    def save_employee_attendance(employee_id):
        employee = db.get_or_404(Employee, employee_id)
        on_date = day("on_date")
        status = choice("status", ATTENDANCE)
        row = db.session.scalar(db.select(EmployeeAttendance).filter_by(employee_id=employee.id, on_date=on_date))
        if row:
            row.status = status
            row.remarks = optional("remarks", 200)
        else:
            db.session.add(EmployeeAttendance(employee_id=employee.id, on_date=on_date,
                                              status=status, remarks=optional("remarks", 200)))
        audit("attendance", "employee", employee.id, on_date.isoformat())
        db.session.commit()
        flash("Attendance recorded.", "success")
        return redirect(url_for("employee_detail", employee_id=employee.id))

    @app.get("/users")
    @role_required("admin")
    def users():
        return render_template("users.html", users=db.session.scalars(db.select(User).order_by(User.created_at.desc())).all(),
                               students=db.session.scalars(db.select(Student).where(Student.user_id.is_(None)).order_by(Student.last_name)).all(),
                               employees=db.session.scalars(db.select(Employee).where(Employee.user_id.is_(None)).order_by(Employee.last_name)).all(),
                               roles=sorted(ROLES))

    @app.post("/users")
    @role_required("admin")
    def create_user():
        email = email_address()
        name = required("full_name", 160)
        role = choice("role", ROLES)
        password = required("password", 500)
        if len(password) < 12:
            raise InputError("Password must have at least 12 characters.")
        student_id = optional_id("student_id")
        employee_id = optional_id("employee_id")
        if role == "student" and not student_id:
            raise InputError("Student accounts must be linked to a student record.")
        if role == "teacher" and not employee_id:
            raise InputError("Teacher accounts must be linked to an employee record.")
        if role != "student" and student_id:
            raise InputError("Only student accounts can be linked to students.")
        user = User(email=email, full_name=name, role=role, password_hash=generate_password_hash(password))
        if student_id:
            student = db.get_or_404(Student, student_id)
            if student.user_id:
                raise InputError("Student already has an account.")
            student.user = user
        if employee_id:
            employee = db.get_or_404(Employee, employee_id)
            if employee.user_id:
                raise InputError("Employee already has an account.")
            employee.user = user
        db.session.add(user)
        db.session.flush()
        audit("create", "user", user.id, role)
        notify("Your account is ready", "Your school workspace account has been created.",
               url_for("account"), user_ids=[user.id], kind="success")
        db.session.commit()
        flash("User account created.", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/toggle")
    @role_required("admin")
    def toggle_user(user_id):
        user = db.get_or_404(User, user_id)
        if user.id == g.user.id:
            raise InputError("You cannot disable your own account.")
        user.is_active = not user.is_active
        audit("toggle", "user", user.id, "active" if user.is_active else "inactive")
        db.session.commit()
        flash("Account status changed.", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/reset-password")
    @role_required("admin")
    def reset_user_password(user_id):
        user = db.get_or_404(User, user_id)
        password = required("password", 500)
        if len(password) < 12:
            raise InputError("Password must have at least 12 characters.")
        user.password_hash = generate_password_hash(password)
        audit("reset_password", "user", user.id)
        db.session.commit()
        flash("Password reset.", "success")
        return redirect(url_for("users"))

    @app.get("/reports")
    @role_required("admin")
    def reports():
        charges = db.session.scalars(db.select(FeeCharge)).all()
        billed = sum((c.amount for c in charges if not c.voided), Decimal("0"))
        paid = sum((c.paid for c in charges if not c.voided), Decimal("0"))
        applications_by_status = dict(db.session.execute(db.select(Applicant.status, func.count(Applicant.id)).group_by(Applicant.status)).all())
        student_attendance = dict(db.session.execute(db.select(StudentAttendance.status, func.count(StudentAttendance.id)).group_by(StudentAttendance.status)).all())
        employee_attendance = dict(db.session.execute(db.select(EmployeeAttendance.status, func.count(EmployeeAttendance.id)).group_by(EmployeeAttendance.status)).all())
        duplicate_emails = db.session.execute(db.select(Student.email, func.count(Student.id)).group_by(Student.email).having(func.count(Student.id) > 1)).all()
        active_users = db.session.scalar(db.select(func.count(User.id)).where(User.is_active.is_(True))) or 0
        logged_in_users = db.session.scalar(db.select(func.count(User.id)).where(User.is_active.is_(True), User.last_login_at.is_not(None))) or 0
        return render_template("reports.html", billed=billed, paid=paid, balance=billed-paid,
                               applications_by_status=applications_by_status,
                               student_attendance=student_attendance, employee_attendance=employee_attendance,
                               duplicates=duplicate_emails, active_users=active_users, logged_in_users=logged_in_users,
                               student_count=db.session.scalar(db.select(func.count(Student.id))) or 0,
                               section_count=db.session.scalar(db.select(func.count(Section.id))) or 0)

    @app.get("/reports/export/<kind>")
    @role_required("admin")
    def export_report(kind):
        output = io.StringIO()
        writer = csv.writer(output)
        if kind == "students":
            writer.writerow(["student_number", "first_name", "last_name", "email", "phone", "program", "year_level", "status"])
            for item in db.session.scalars(db.select(Student).order_by(Student.id)):
                writer.writerow([item.student_number, item.first_name, item.last_name, item.email, item.phone, item.program, item.year_level, item.status])
        elif kind == "applications":
            writer.writerow(["reference", "first_name", "last_name", "email", "phone", "program", "status", "created_at"])
            for item in db.session.scalars(db.select(Applicant).order_by(Applicant.id)):
                writer.writerow([item.reference, item.first_name, item.last_name, item.email, item.phone, item.program, item.status, item.created_at.isoformat()])
        elif kind == "finance":
            writer.writerow(["student_number", "student_name", "description", "amount", "paid", "balance", "due_on", "status"])
            for item in db.session.scalars(db.select(FeeCharge).order_by(FeeCharge.id)):
                writer.writerow([item.student.student_number, item.student.full_name, item.description, item.amount, item.paid, item.balance, item.due_on.isoformat() if item.due_on else "", "voided" if item.voided else "active"])
        elif kind == "employees":
            writer.writerow(["employee_number", "first_name", "last_name", "email", "department", "position", "status"])
            for item in db.session.scalars(db.select(Employee).order_by(Employee.id)):
                writer.writerow([item.employee_number, item.first_name, item.last_name, item.email, item.department, item.position, item.status])
        else:
            abort(404)
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename=isms-{kind}.csv"
        return response

    @app.get("/activity")
    @role_required("admin")
    def activity():
        logs = db.session.scalars(db.select(AuditLog).order_by(AuditLog.created_at.desc()).limit(150)).all()
        return render_template("activity.html", logs=logs)

    return app


# Vercel and other WSGI hosts discover this root-level Flask application.
app = create_app()


if __name__ == "__main__":
    # Running app.py from an IDE starts a local server without requiring Flask CLI commands.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        with app.app_context():
            db.create_all()
            if not db.session.scalar(db.select(User.id).limit(1)):
                click.echo("No administrator account exists yet. In the PyCharm Terminal, run:")
                click.echo("  .\\.venv\\Scripts\\python.exe -m flask --app app create-admin")
    port = int(os.getenv("PORT", "5000"))
    click.echo(f"Open http://127.0.0.1:{port} in your browser.")
    app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
