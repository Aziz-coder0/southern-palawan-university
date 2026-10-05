from datetime import datetime, timezone
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime(timezone=True))
    notifications = db.relationship("Notification", backref="user", cascade="all, delete-orphan")


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    attempted_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class Applicant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(24), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    phone = db.Column(db.String(40), nullable=False)
    program = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, default="", nullable=False)
    status = db.Column(db.String(20), nullable=False, default="submitted")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    requirements = db.relationship("ApplicationRequirement", backref="applicant", cascade="all, delete-orphan", order_by="ApplicationRequirement.id")
    profile = db.relationship("ApplicantProfile", backref="applicant", uselist=False, cascade="all, delete-orphan")
    academic_grades = db.relationship("ApplicantAcademicGrade", backref="applicant", cascade="all, delete-orphan", order_by="ApplicantAcademicGrade.grade_level, ApplicantAcademicGrade.id")
    academic_document = db.relationship("ApplicantDocument", backref="applicant", uselist=False, cascade="all, delete-orphan")
    academic_review = db.relationship("ApplicantAcademicReview", backref="applicant", uselist=False, cascade="all, delete-orphan")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class ApplicantProfile(db.Model):
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), primary_key=True)
    applicant_type = db.Column(db.String(20), nullable=False)
    middle_name = db.Column(db.String(80), nullable=False, default="")
    birth_date = db.Column(db.Date, nullable=False)
    home_address = db.Column(db.String(300), nullable=False)
    previous_school = db.Column(db.String(180), nullable=False)
    last_school_year = db.Column(db.String(20), nullable=False, default="")
    emergency_name = db.Column(db.String(160), nullable=False)
    emergency_relationship = db.Column(db.String(60), nullable=False)
    emergency_phone = db.Column(db.String(40), nullable=False)


class ApplicantAcademicGrade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), nullable=False, index=True)
    grade_level = db.Column(db.Integer, nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    final_grade = db.Column(db.Numeric(5, 2), nullable=False)
    __table_args__ = (CheckConstraint("grade_level BETWEEN 8 AND 11"),
                      CheckConstraint("final_grade >= 0 AND final_grade <= 100"),)


class ApplicantDocument(db.Model):
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), primary_key=True)
    filename = db.Column(db.String(180), nullable=False)
    content_type = db.Column(db.String(40), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    uploaded_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class ApplicantAcademicReview(db.Model):
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), primary_key=True)
    status = db.Column(db.String(24), nullable=False, default="pending")
    note = db.Column(db.String(500), nullable=False, default="")
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    reviewer = db.relationship("User")


class ApplicationRequirement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    received = db.Column(db.Boolean, default=False, nullable=False)
    received_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("applicant_id", "name"),)


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicant.id"), unique=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(40), nullable=False, default="")
    program = db.Column(db.String(120), nullable=False)
    year_level = db.Column(db.String(20), nullable=False, default="1")
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    applicant = db.relationship("Applicant", backref=db.backref("student", uselist=False))
    user = db.relationship("User", backref=db.backref("student", uselist=False))

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(40), nullable=False, default="")
    department = db.Column(db.String(100), nullable=False)
    position = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active")
    hired_on = db.Column(db.Date)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user = db.relationship("User", backref=db.backref("employee", uselist=False))

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    units = db.Column(db.Integer, nullable=False, default=3)
    description = db.Column(db.Text, default="", nullable=False)


class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    school_year = db.Column(db.String(20), nullable=False)
    term = db.Column(db.String(30), nullable=False)
    schedule = db.Column(db.String(120), nullable=False)
    room = db.Column(db.String(60), default="", nullable=False)
    capacity = db.Column(db.Integer, default=40, nullable=False)
    subject = db.relationship("Subject", backref="sections")
    teacher = db.relationship("Employee", backref="sections")
    enrollments = db.relationship("Enrollment", backref="section")
    __table_args__ = (UniqueConstraint("code", "school_year", "term"), CheckConstraint("capacity > 0"))


class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="enrolled")
    enrolled_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    student = db.relationship("Student", backref="enrollments")
    __table_args__ = (UniqueConstraint("student_id", "section_id"),)


class StudentAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    on_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(12), nullable=False)
    remarks = db.Column(db.String(200), default="", nullable=False)
    student = db.relationship("Student")
    section = db.relationship("Section")
    __table_args__ = (UniqueConstraint("student_id", "section_id", "on_date"),)


class Grade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    period = db.Column(db.String(30), nullable=False)
    score = db.Column(db.Numeric(5, 2), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    student = db.relationship("Student", backref="grades")
    section = db.relationship("Section")
    __table_args__ = (UniqueConstraint("student_id", "section_id", "period"), CheckConstraint("score >= 0 AND score <= 100"))


class FeeCharge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    description = db.Column(db.String(160), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    voided = db.Column(db.Boolean, default=False, nullable=False)
    due_on = db.Column(db.Date)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    student = db.relationship("Student", backref="charges")
    payments = db.relationship("Payment", backref="charge")
    __table_args__ = (CheckConstraint("amount > 0"),)

    @property
    def paid(self):
        return sum((p.amount for p in self.payments if not p.voided), Decimal("0.00"))

    @property
    def balance(self):
        return Decimal("0.00") if self.voided else self.amount - self.paid


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    charge_id = db.Column(db.Integer, db.ForeignKey("fee_charge.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    method = db.Column(db.String(30), nullable=False)
    reference = db.Column(db.String(80), nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    voided = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (CheckConstraint("amount > 0"),)


class EmployeeAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    on_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(12), nullable=False)
    remarks = db.Column(db.String(200), default="", nullable=False)
    employee = db.relationship("Employee", backref="attendance")
    __table_args__ = (UniqueConstraint("employee_id", "on_date"),)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(80), nullable=False)
    entity = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer)
    detail = db.Column(db.String(300), default="", nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user = db.relationship("User")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.String(260), nullable=False)
    link = db.Column(db.String(255), nullable=False, default="/dashboard")
    kind = db.Column(db.String(20), nullable=False, default="info")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    read_at = db.Column(db.DateTime(timezone=True))
