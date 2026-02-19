"""SQLAlchemy Models für das Matching-Tool."""

from app.models.alert import Alert
from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_call_note import ATSCallNote, CallType
from app.models.ats_email_template import ATSEmailTemplate
from app.models.ats_job import ATSJob, ATSJobPriority, ATSJobStatus
from app.models.ats_pipeline import ATSPipelineEntry, PipelineStage
from app.models.ats_todo import ATSTodo, TodoPriority, TodoStatus
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote
from app.models.email_draft import EmailDraft, EmailDraftStatus, EmailType
from app.models.candidate_email import CandidateEmail
from app.models.candidate_task import CandidateTask
from app.models.outreach_batch import OutreachBatch
from app.models.outreach_item import OutreachItem
from app.models.company import Company, CompanyStatus
from app.models.company_contact import CompanyContact
from app.models.company_correspondence import CompanyCorrespondence, CorrespondenceDirection
from app.models.company_document import CompanyDocument
from app.models.company_note import CompanyNote
from app.models.import_job import ImportJob
from app.models.job import Job
from app.models.job_run import JobRun
from app.models.match import Match
from app.models.match_v2_models import MatchV2LearnedRule, MatchV2ScoringWeight, MatchV2TrainingData
from app.models.mt_match_memory import MTMatchMemory
from app.models.mt_training import MTTrainingData
from app.models.settings import FilterPreset, PriorityCity
from app.models.statistics import DailyStatistics, FilterUsage
from app.models.unassigned_call import UnassignedCall

__all__ = [
    "Job",
    "Candidate",
    "Match",
    "Company",
    "CompanyStatus",
    "CompanyContact",
    "CompanyCorrespondence",
    "CorrespondenceDirection",
    "CompanyDocument",
    "CompanyNote",
    "PriorityCity",
    "FilterPreset",
    "DailyStatistics",
    "FilterUsage",
    "Alert",
    "ImportJob",
    "JobRun",
    "MTTrainingData",
    "MTMatchMemory",
    "ATSJob",
    "ATSJobPriority",
    "ATSJobStatus",
    "ATSPipelineEntry",
    "PipelineStage",
    "ATSCallNote",
    "CallType",
    "ATSTodo",
    "TodoStatus",
    "TodoPriority",
    "ATSActivity",
    "ActivityType",
    "ATSEmailTemplate",
    "MatchV2TrainingData",
    "MatchV2LearnedRule",
    "MatchV2ScoringWeight",
    "UnassignedCall",
    "CandidateNote",
    "EmailDraft",
    "EmailDraftStatus",
    "EmailType",
    "CandidateEmail",
    "CandidateTask",
    "OutreachBatch",
    "OutreachItem",
]
