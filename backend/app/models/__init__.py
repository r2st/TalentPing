"""SQLAlchemy models.

Importing this package registers every model on the shared ``Base.metadata`` so
that ``Base.metadata.create_all`` and Alembic autogenerate see them all.
"""
from app.models.app_credential import AppCredential
from app.models.application import Application, ApplicationStatus
from app.models.ats_board import AtsBoard
from app.models.autopilot import AutopilotPreference
from app.models.campaign import Campaign, CampaignStatus
from app.models.company_profile import CompanyProfile
from app.models.cover_letter import CoverLetter
from app.models.dead_letter import (
    REASON_FAILED,
    REASON_REVOKED,
    STATUS_IGNORED,
    STATUS_NEW,
    STATUS_REPLAYED,
    STATUSES,
    DeadLetterJob,
)
from app.models.digest import DigestPreference
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_attachment import EmailAttachment
from app.models.email_bounce import BounceKind, EmailBounce
from app.models.email_event import EmailEvent, EmailEventType
from app.models.email_thread import EmailThread
from app.models.feature_event import FeatureEvent
from app.models.fit_score import FitScore
from app.models.follow_up import FollowUp, FollowUpStatus, FollowUpTemplate
from app.models.form_apply import (
    ATSPlatform,
    FormApplication,
    FormApplyProfile,
    FormApplyStatus,
)
from app.models.gmail_account import GmailAccount
from app.models.gmail_watch import GmailWatch
from app.models.job import JobPosting, JobSearch, JobStatus, job_fingerprint
from app.models.linkedin_account import LinkedInAccount
from app.models.notification import (
    NOTIFICATION_KINDS,
    SEVERITIES,
    Notification,
    NotificationKind,
    NotificationPreference,
    NotificationSeverity,
)
from app.models.profile import Profile
from app.models.recruiter import DeliveryState, Recruiter
from app.models.recruiter_cache import RecruiterCache
from app.models.recruiter_email import (
    ACTIONABLE_KINDS,
    RecruiterEmail,
    RecruiterEmailKind,
    RecruiterEmailStatus,
    RecruiterReplyPreference,
    ReplyRoute,
)
from app.models.recruiter_scan_run import (
    TRIGGER_BACKLOG,
    TRIGGER_BEAT,
    TRIGGER_MANUAL,
    TRIGGER_PUSH,
    RecruiterScanRun,
)
from app.models.recruiter_scan_skip import (
    REASON_BLOCKED_SENDER,
    REASON_FROM_SELF,
    RecruiterScanSkip,
)
from app.models.reply_feedback import (
    ClassifierPrior,
    FeedbackSignal,
    PriorScope,
    ReplyFeedback,
)
from app.models.resume import Resume
from app.models.salary_benchmark import SalaryBenchmark
from app.models.status_event import ApplicationStatusEvent, StatusEventSource
from app.models.subject_variant import SubjectVariant
from app.models.tailored_resume import TailoredResume
from app.models.user import User

__all__ = [
    "AppCredential",
    "Application",
    "ApplicationStatus",
    "ApplicationStatusEvent",
    "AtsBoard",
    "AutopilotPreference",
    "BounceKind",
    "Campaign",
    "CampaignStatus",
    "CompanyProfile",
    "CoverLetter",
    "DeadLetterJob",
    "REASON_FAILED",
    "REASON_REVOKED",
    "STATUSES",
    "STATUS_IGNORED",
    "STATUS_NEW",
    "STATUS_REPLAYED",
    "DeliveryState",
    "DigestPreference",
    "Email",
    "EmailAttachment",
    "EmailBounce",
    "EmailDirection",
    "EmailEvent",
    "EmailEventType",
    "EmailStatus",
    "EmailThread",
    "FeatureEvent",
    "ATSPlatform",
    "FitScore",
    "FollowUp",
    "FollowUpStatus",
    "FollowUpTemplate",
    "FormApplication",
    "FormApplyProfile",
    "FormApplyStatus",
    "GmailAccount",
    "GmailWatch",
    "JobPosting",
    "JobSearch",
    "JobStatus",
    "LinkedInAccount",
    "NOTIFICATION_KINDS",
    "Notification",
    "NotificationKind",
    "NotificationPreference",
    "NotificationSeverity",
    "SEVERITIES",
    "Profile",
    "ACTIONABLE_KINDS",
    "ClassifierPrior",
    "FeedbackSignal",
    "PriorScope",
    "Recruiter",
    "RecruiterCache",
    "RecruiterEmail",
    "RecruiterEmailKind",
    "RecruiterEmailStatus",
    "RecruiterReplyPreference",
    "RecruiterScanRun",
    "RecruiterScanSkip",
    "REASON_BLOCKED_SENDER",
    "REASON_FROM_SELF",
    "ReplyFeedback",
    "ReplyIntent",
    "ReplyRoute",
    "TRIGGER_BACKLOG",
    "TRIGGER_BEAT",
    "TRIGGER_MANUAL",
    "TRIGGER_PUSH",
    "Resume",
    "SalaryBenchmark",
    "StatusEventSource",
    "SubjectVariant",
    "TailoredResume",
    "User",
    "job_fingerprint",
]
