"""SQLAlchemy Models für das Matching-Tool."""

from app.models.alert import Alert
from app.models.candidate import Candidate
from app.models.company import Company, CompanyStatus
from app.models.company_contact import CompanyContact
from app.models.company_correspondence import CompanyCorrespondence, CorrespondenceDirection
from app.models.import_job import ImportJob
from app.models.job import Job
from app.models.job_run import JobRun
from app.models.match import Match
from app.models.settings import FilterPreset, PriorityCity
from app.models.statistics import DailyStatistics, FilterUsage

__all__ = [
    "Job",
    "Candidate",
    "Match",
    "Company",
    "CompanyStatus",
    "CompanyContact",
    "CompanyCorrespondence",
    "CorrespondenceDirection",
    "PriorityCity",
    "FilterPreset",
    "DailyStatistics",
    "FilterUsage",
    "Alert",
    "ImportJob",
    "JobRun",
]
