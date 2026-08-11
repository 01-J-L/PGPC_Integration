# website/faculty/models.py
#
# Backwards-compatibility re-export. All model definitions now live in
# the single source of truth: website/models.py

from website.models import (
    Program, Course, Section, User, TeachingLoad, Announcement, TimeSlot,
    SubstitutionRequest, Notification, SystemSetting, Room,
    Subject, Prerequisite, ProgramObjective, ProgramCareer, ProgramCourse,
    AcademicTerm, AdmissionStep, StudentEnrollmentRecord, SystemUser,
    StudentUser, Applicant, ApplicationMessage, StudentGrade,
    GradingSettings, Assessment, BatchAssessmentLog, ChartOfAccount, Payment,
    OrganizationDiscount, StudentDiscountApplied, VoucherReturn, JournalEntry,
    JournalLine, StudentAssessment, StudentPayment, FacultyAssignment, Grade,
    PageContent, HeroSlide, NewsArticle, FacultyMember, FAQ, StatusRequirement,
    StatusUpload, SocialMediaLink, StudentDownload, ReceiptLayoutSetting,
)

__all__ = [
    'Program', 'Course', 'Section', 'User', 'TeachingLoad', 'Announcement',
    'TimeSlot', 'SubstitutionRequest', 'Notification', 'SystemSetting', 'Room',
    'Subject', 'Prerequisite', 'ProgramObjective', 'ProgramCareer',
    'ProgramCourse', 'AcademicTerm', 'AdmissionStep', 'StudentEnrollmentRecord',
    'SystemUser', 'StudentUser', 'Applicant', 'ApplicationMessage',
    'StudentGrade', 'GradingSettings', 'Assessment', 'BatchAssessmentLog',
    'ChartOfAccount', 'Payment', 'OrganizationDiscount',
    'StudentDiscountApplied', 'VoucherReturn', 'JournalEntry', 'JournalLine',
    'StudentAssessment', 'StudentPayment', 'FacultyAssignment', 'Grade',
    'PageContent', 'HeroSlide', 'NewsArticle', 'FacultyMember', 'FAQ',
    'StatusRequirement', 'StatusUpload', 'SocialMediaLink', 'StudentDownload',
    'ReceiptLayoutSetting',
]
