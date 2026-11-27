-- =====================================================================
--  PGPC Integration  --  FULL DATABASE SCRIPT  (MySQL 8 / MariaDB 10.4+)
--  Padre Garcia Polytechnic College: Admission + Faculty + Grading +
--  Accounting, all in ONE database.
--
--  Generated from website/models.py (49 tables). Safe to re-run:
--  every table uses CREATE TABLE IF NOT EXISTS and the seed data uses
--  ON DUPLICATE KEY UPDATE / WHERE NOT EXISTS.
--
--  HOW TO RUN
--    mysql -u <user> -p < pgpc_full_database.sql
--  The database name below must match DB_NAME in your .env file.
--
--  NOTES
--   * The Flask app ALSO creates missing tables at startup (db.create_all()),
--     so this script is optional for a fresh deploy - use it to build the DB
--     ahead of time, on a server where the app user cannot CREATE, or to
--     document the schema.
--   * Python-side defaults from the models (role='faculty', status='pending',
--     can_edit=1, ...) are written here as real MySQL DEFAULTs, so raw SQL
--     inserts that omit those columns also work.
--   * No user accounts are inserted. The 'admin' account is created
--     automatically at first login using ADMIN_INITIAL_PASSWORD from .env.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `pgpc_db`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `pgpc_db`;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
--  TABLE LIST (creation order = foreign-key dependency order)
--    1. academic_terms
--    2. admission_steps
--    3. batch_assessments_log
--    4. chart_of_accounts
--    5. faculty
--    6. faqs
--    7. grading_settings
--    8. hero_slides
--    9. journal_entries
--   10. news_articles
--   11. organization_discounts
--   12. page_content
--   13. programs
--   14. receipt_layout_settings
--   15. room
--   16. social_media_links
--   17. status_requirements
--   18. status_uploads
--   19. student_downloads
--   20. student_users
--   21. system_settings
--   22. system_users
--   23. time_slots
--   24. courses
--   25. journal_lines
--   26. program_careers
--   27. program_courses
--   28. program_objectives
--   29. sections
--   30. student_assessments
--   31. student_enrollment_records
--   32. subjects
--   33. users
--   34. announcements
--   35. applicants
--   36. faculty_assignments
--   37. prerequisites
--   38. student_grades
--   39. student_payments
--   40. subject_prerequisites
--   41. teaching_loads
--   42. application_messages
--   43. assessments
--   44. grades
--   45. student_discounts_applied
--   46. substitution_requests
--   47. voucher_returns
--   48. notifications
--   49. payments
-- ---------------------------------------------------------------------

-- ---- academic_terms ----------------------------------------------
CREATE TABLE IF NOT EXISTS academic_terms (
	id INTEGER NOT NULL AUTO_INCREMENT,
	year_name VARCHAR(50) NOT NULL,
	semester VARCHAR(50) NOT NULL,
	is_active BOOL NOT NULL DEFAULT 0,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	max_units INTEGER DEFAULT 26,
	PRIMARY KEY (id),
	CONSTRAINT unique_term UNIQUE (year_name, semester)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- admission_steps ---------------------------------------------
CREATE TABLE IF NOT EXISTS admission_steps (
	id INTEGER NOT NULL AUTO_INCREMENT,
	step_number INTEGER NOT NULL,
	title VARCHAR(255) NOT NULL,
	description TEXT,
	icon_class VARCHAR(50) DEFAULT 'ri-number-1',
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- batch_assessments_log ---------------------------------------
CREATE TABLE IF NOT EXISTS batch_assessments_log (
	id INTEGER NOT NULL AUTO_INCREMENT,
	description VARCHAR(255),
	amount NUMERIC(10, 2),
	target_year VARCHAR(50),
	semester VARCHAR(50),
	course VARCHAR(100),
	year_level VARCHAR(50),
	scope VARCHAR(50),
	student_count INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- chart_of_accounts -------------------------------------------
CREATE TABLE IF NOT EXISTS chart_of_accounts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	account_code VARCHAR(20) NOT NULL,
	account_name VARCHAR(200) NOT NULL,
	type VARCHAR(30) NOT NULL,
	is_active BOOL DEFAULT 1,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- faculty -----------------------------------------------------
CREATE TABLE IF NOT EXISTS faculty (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(255) NOT NULL,
	`role` VARCHAR(255) NOT NULL,
	image_filename VARCHAR(255),
	category VARCHAR(20) NOT NULL,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- faqs --------------------------------------------------------
CREATE TABLE IF NOT EXISTS faqs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	question TEXT NOT NULL,
	answer TEXT NOT NULL,
	sort_order INTEGER DEFAULT 0,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- grading_settings --------------------------------------------
CREATE TABLE IF NOT EXISTS grading_settings (
	id INTEGER NOT NULL AUTO_INCREMENT,
	active_school_year VARCHAR(20) NOT NULL DEFAULT '2024-2025',
	active_semester VARCHAR(20) NOT NULL DEFAULT '1st Semester',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- hero_slides -------------------------------------------------
CREATE TABLE IF NOT EXISTS hero_slides (
	id INTEGER NOT NULL AUTO_INCREMENT,
	image_filename VARCHAR(255),
	intro_text VARCHAR(255),
	title_text VARCHAR(255),
	highlight_text VARCHAR(255),
	button_text VARCHAR(50) DEFAULT 'Learn More',
	button_link VARCHAR(255) DEFAULT '#',
	sort_order INTEGER DEFAULT 0,
	is_active BOOL DEFAULT 1,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- journal_entries ---------------------------------------------
CREATE TABLE IF NOT EXISTS journal_entries (
	id INTEGER NOT NULL AUTO_INCREMENT,
	entry_date DATE NOT NULL,
	description TEXT,
	reference_number VARCHAR(50),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	created_by INTEGER,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- news_articles -----------------------------------------------
CREATE TABLE IF NOT EXISTS news_articles (
	id INTEGER NOT NULL AUTO_INCREMENT,
	title VARCHAR(255) NOT NULL,
	publish_date DATE,
	image_filename VARCHAR(255),
	description TEXT,
	read_more_link VARCHAR(255),
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- organization_discounts --------------------------------------
CREATE TABLE IF NOT EXISTS organization_discounts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(100) NOT NULL,
	discount_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.0,
	PRIMARY KEY (id),
	UNIQUE (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- page_content ------------------------------------------------
CREATE TABLE IF NOT EXISTS page_content (
	content_key VARCHAR(50) NOT NULL,
	content_value TEXT,
	PRIMARY KEY (content_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- programs ----------------------------------------------------
CREATE TABLE IF NOT EXISTS programs (
	program_id VARCHAR(50) NOT NULL,
	title VARCHAR(255) NOT NULL,
	hero_image_filename VARCHAR(255),
	description TEXT,
	admission_link_endpoint VARCHAR(255) DEFAULT 'views.existing_or_not',
	total_units INTEGER DEFAULT 0,
	max_units INTEGER DEFAULT 26,
	program_code VARCHAR(20),
	program_name VARCHAR(200),
	PRIMARY KEY (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- receipt_layout_settings -------------------------------------
CREATE TABLE IF NOT EXISTS receipt_layout_settings (
	field_id VARCHAR(50) NOT NULL,
	top_pos VARCHAR(20) DEFAULT '0%%',
	left_pos VARCHAR(20) DEFAULT '0%%',
	font_size VARCHAR(10) DEFAULT '14px',
	is_visible BOOL DEFAULT 1,
	PRIMARY KEY (field_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- room --------------------------------------------------------
CREATE TABLE IF NOT EXISTS room (
	id INTEGER NOT NULL AUTO_INCREMENT,
	room_name VARCHAR(50) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (room_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- social_media_links ------------------------------------------
CREATE TABLE IF NOT EXISTS social_media_links (
	id INTEGER NOT NULL AUTO_INCREMENT,
	platform_name VARCHAR(50) NOT NULL,
	url VARCHAR(255) NOT NULL,
	icon_class VARCHAR(50) NOT NULL,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- status_requirements -----------------------------------------
CREATE TABLE IF NOT EXISTS status_requirements (
	id INTEGER NOT NULL AUTO_INCREMENT,
	status_key VARCHAR(50) NOT NULL,
	requirement_text VARCHAR(255) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- status_uploads ----------------------------------------------
CREATE TABLE IF NOT EXISTS status_uploads (
	id INTEGER NOT NULL AUTO_INCREMENT,
	status_key VARCHAR(50) NOT NULL,
	program_id VARCHAR(50),
	file_filename VARCHAR(255) NOT NULL,
	display_name VARCHAR(255) NOT NULL,
	file_type VARCHAR(50),
	uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_downloads -------------------------------------------
CREATE TABLE IF NOT EXISTS student_downloads (
	id INTEGER NOT NULL AUTO_INCREMENT,
	category VARCHAR(50) NOT NULL,
	title VARCHAR(100) NOT NULL,
	filename VARCHAR(255) NOT NULL,
	uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	target_program VARCHAR(100) DEFAULT 'All',
	target_year_level VARCHAR(50) DEFAULT 'All',
	target_section VARCHAR(100) DEFAULT 'All',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_users -----------------------------------------------
CREATE TABLE IF NOT EXISTS student_users (
	id INTEGER NOT NULL AUTO_INCREMENT,
	email VARCHAR(255) NOT NULL,
	password VARCHAR(255) NOT NULL,
	otp_code VARCHAR(10),
	otp_expiry DATETIME,
	is_verified BOOL NOT NULL DEFAULT 0,
	student_type VARCHAR(10) NOT NULL DEFAULT 'new',
	old_student_id VARCHAR(255),
	reset_token VARCHAR(128),
	reset_token_expiry DATETIME,
	created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	UNIQUE (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- system_settings ---------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
	setting_key VARCHAR(50) NOT NULL,
	setting_value VARCHAR(255) NOT NULL,
	PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- system_users ------------------------------------------------
CREATE TABLE IF NOT EXISTS system_users (
	id INTEGER NOT NULL AUTO_INCREMENT,
	username VARCHAR(50) NOT NULL,
	password VARCHAR(255) NOT NULL,
	`role` VARCHAR(20) NOT NULL,
	full_name VARCHAR(100),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	can_edit BOOL DEFAULT 1,
	department VARCHAR(100),
	email VARCHAR(100),
	`system` VARCHAR(20) DEFAULT 'admission',
	PRIMARY KEY (id),
	UNIQUE (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- time_slots --------------------------------------------------
CREATE TABLE IF NOT EXISTS time_slots (
	id INTEGER NOT NULL AUTO_INCREMENT,
	start_time TIME NOT NULL,
	end_time TIME NOT NULL,
	is_default BOOL NOT NULL DEFAULT 0,
	PRIMARY KEY (id),
	UNIQUE (start_time),
	UNIQUE (end_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- courses -----------------------------------------------------
CREATE TABLE IF NOT EXISTS courses (
	id INTEGER NOT NULL AUTO_INCREMENT,
	program_id VARCHAR(50) NOT NULL,
	course_code VARCHAR(20) NOT NULL,
	course_description VARCHAR(200) NOT NULL,
	units INTEGER NOT NULL,
	year INTEGER NOT NULL,
	semester INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(program_id) REFERENCES programs (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- journal_lines -----------------------------------------------
CREATE TABLE IF NOT EXISTS journal_lines (
	id INTEGER NOT NULL AUTO_INCREMENT,
	journal_entry_id INTEGER NOT NULL,
	account_id INTEGER NOT NULL,
	debit NUMERIC(15, 2) DEFAULT 0.0,
	credit NUMERIC(15, 2) DEFAULT 0.0,
	PRIMARY KEY (id),
	FOREIGN KEY(journal_entry_id) REFERENCES journal_entries (id),
	FOREIGN KEY(account_id) REFERENCES chart_of_accounts (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- program_careers ---------------------------------------------
CREATE TABLE IF NOT EXISTS program_careers (
	id INTEGER NOT NULL AUTO_INCREMENT,
	program_id VARCHAR(50) NOT NULL,
	career_text VARCHAR(255) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(program_id) REFERENCES programs (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- program_courses ---------------------------------------------
CREATE TABLE IF NOT EXISTS program_courses (
	id INTEGER NOT NULL AUTO_INCREMENT,
	program_id VARCHAR(50) NOT NULL,
	course_code VARCHAR(50),
	course_name VARCHAR(255) NOT NULL,
	course_description TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(program_id) REFERENCES programs (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- program_objectives ------------------------------------------
CREATE TABLE IF NOT EXISTS program_objectives (
	id INTEGER NOT NULL AUTO_INCREMENT,
	program_id VARCHAR(50) NOT NULL,
	objective_text TEXT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(program_id) REFERENCES programs (program_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- sections ----------------------------------------------------
CREATE TABLE IF NOT EXISTS sections (
	id INTEGER NOT NULL AUTO_INCREMENT,
	program_id VARCHAR(50) NOT NULL,
	year_level VARCHAR(20) NOT NULL,
	section_name VARCHAR(50) NOT NULL,
	section_letter VARCHAR(5) NOT NULL,
	max_students INTEGER DEFAULT 60,
	current_students INTEGER DEFAULT 0,
	is_active BOOL DEFAULT 1,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	semester INTEGER,
	school_year VARCHAR(9),
	PRIMARY KEY (id),
	UNIQUE (program_id, year_level, section_name),
	FOREIGN KEY(program_id) REFERENCES programs (program_id),
	INDEX ix_sections_school_year (school_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_assessments -----------------------------------------
CREATE TABLE IF NOT EXISTS student_assessments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_user_id INTEGER NOT NULL,
	description VARCHAR(200) NOT NULL,
	total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
	amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
	balance NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
	status VARCHAR(20) DEFAULT 'unpaid',
	academic_year VARCHAR(20),
	semester VARCHAR(20),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(student_user_id) REFERENCES student_users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_enrollment_records ----------------------------------
CREATE TABLE IF NOT EXISTS student_enrollment_records (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_user_id INTEGER NOT NULL,
	academic_term_id INTEGER NOT NULL,
	year_level VARCHAR(50),
	enrollment_semester VARCHAR(50),
	enrollment_status VARCHAR(50) DEFAULT 'Enrolling',
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	is_approved_by_president BOOL DEFAULT 0,
	president_approval_date DATETIME,
	is_approved_by_accounting BOOL DEFAULT 0,
	accounting_approval_date DATETIME,
	is_approved_by_dean BOOL DEFAULT 0,
	dean_approval_date DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT unique_enrollment UNIQUE (student_user_id, academic_term_id),
	FOREIGN KEY(student_user_id) REFERENCES student_users (id),
	FOREIGN KEY(academic_term_id) REFERENCES academic_terms (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- subjects ----------------------------------------------------
CREATE TABLE IF NOT EXISTS subjects (
	id INTEGER NOT NULL AUTO_INCREMENT,
	subject_code VARCHAR(20) NOT NULL,
	subject_title VARCHAR(255) NOT NULL,
	program_id VARCHAR(50) NOT NULL,
	units NUMERIC(3, 1) DEFAULT 3.0,
	semester INTEGER,
	prerequisite_subject_id INTEGER,
	year_level INTEGER,
	PRIMARY KEY (id),
	UNIQUE (subject_code),
	FOREIGN KEY(program_id) REFERENCES programs (program_id),
	FOREIGN KEY(prerequisite_subject_id) REFERENCES subjects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- users -------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
	id INTEGER NOT NULL AUTO_INCREMENT,
	username VARCHAR(64),
	email VARCHAR(120) NOT NULL,
	password_hash VARCHAR(256),
	`role` VARCHAR(20) NOT NULL DEFAULT 'faculty',
	status VARCHAR(20) NOT NULL DEFAULT 'pending',
	full_name VARCHAR(150) NOT NULL,
	contact_number VARCHAR(20),
	department VARCHAR(100),
	`system` VARCHAR(20) NOT NULL DEFAULT 'faculty',
	can_edit BOOL NOT NULL DEFAULT 1,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	password_hash_alt VARCHAR(256),
	baccalaureate_degree VARCHAR(150),
	baccalaureate_year_grad INTEGER,
	highest_edu_attainment VARCHAR(150),
	highest_edu_year_grad VARCHAR(20),
	industry_experience TEXT,
	is_licensed BOOL DEFAULT 0,
	major VARCHAR(100),
	subjects_taught TEXT,
	otp VARCHAR(6),
	otp_expiration DATETIME,
	hourly_rate FLOAT DEFAULT 0.0,
	expertise_type VARCHAR(10),
	program_id VARCHAR(50),
	PRIMARY KEY (id),
	FOREIGN KEY(program_id) REFERENCES programs (program_id),
	UNIQUE INDEX ix_users_email (email),
	UNIQUE INDEX ix_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- announcements -----------------------------------------------
CREATE TABLE IF NOT EXISTS announcements (
	id INTEGER NOT NULL AUTO_INCREMENT,
	title VARCHAR(255) NOT NULL,
	content TEXT NOT NULL,
	read_more_link VARCHAR(500),
	is_active BOOL NOT NULL DEFAULT 1,
	created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	image_filename VARCHAR(255),
	user_id INTEGER,
	timestamp DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- applicants --------------------------------------------------
CREATE TABLE IF NOT EXISTS applicants (
	applicant_id INTEGER NOT NULL AUTO_INCREMENT,
	student_user_id INTEGER NOT NULL,
	program_choice VARCHAR(255),
	last_name VARCHAR(100) NOT NULL,
	first_name VARCHAR(100) NOT NULL,
	middle_name VARCHAR(100),
	date_of_birth DATE NOT NULL,
	place_of_birth TEXT,
	sex VARCHAR(20) NOT NULL,
	civil_status VARCHAR(50) NOT NULL,
	religion VARCHAR(100) NOT NULL,
	citizenship VARCHAR(100) NOT NULL,
	mobile_number VARCHAR(30) NOT NULL,
	email_address VARCHAR(255) NOT NULL,
	permanent_address_street_barangay TEXT,
	permanent_address_city_municipality VARCHAR(100) NOT NULL,
	permanent_address_province VARCHAR(100) NOT NULL,
	permanent_address_postal_code VARCHAR(20) NOT NULL,
	cultural_minority_group TEXT,
	physical_disability TEXT,
	control_number VARCHAR(50),
	permit_control_no VARCHAR(50),
	old_student_id VARCHAR(255),
	permit_exam_date DATE,
	permit_exam_time VARCHAR(100),
	permit_testing_room VARCHAR(100),
	date_of_application DATE NOT NULL,
	academic_year VARCHAR(20) NOT NULL,
	average_family_income VARCHAR(100) NOT NULL,
	father_name VARCHAR(255),
	father_occupation VARCHAR(100),
	father_company_address TEXT,
	father_contact_number VARCHAR(30),
	mother_maiden_name VARCHAR(255),
	mother_occupation VARCHAR(100),
	mother_company_address TEXT,
	mother_contact_number VARCHAR(30),
	guardian_name VARCHAR(255) NOT NULL,
	guardian_occupation VARCHAR(100) NOT NULL,
	guardian_company_address TEXT,
	guardian_contact_number VARCHAR(30) NOT NULL,
	senior_high_school TEXT,
	senior_high_school_address TEXT,
	senior_high_school_track_strand VARCHAR(100) NOT NULL,
	senior_high_school_year_from VARCHAR(10) NOT NULL,
	senior_high_school_year_to VARCHAR(10) NOT NULL,
	tertiary_school TEXT,
	tertiary_school_address TEXT,
	tertiary_course TEXT,
	tertiary_year_from VARCHAR(10),
	tertiary_year_to VARCHAR(10),
	photo MEDIUMBLOB,
	photo_mimetype VARCHAR(100),
	shs_diploma_file MEDIUMBLOB,
	shs_diploma_filename VARCHAR(255),
	shs_diploma_mimetype VARCHAR(100),
	shs_card_file MEDIUMBLOB,
	shs_card_filename VARCHAR(255),
	shs_card_mimetype VARCHAR(100),
	birth_certificate_file MEDIUMBLOB,
	birth_certificate_filename VARCHAR(255),
	birth_certificate_mimetype VARCHAR(100),
	application_status VARCHAR(50) NOT NULL DEFAULT 'Pending',
	original_enrollment_status VARCHAR(50),
	original_academic_term VARCHAR(100),
	exam_status VARCHAR(50),
	admin_notes TEXT,
	agreements VARCHAR(10) NOT NULL,
	final_submission_date DATE NOT NULL,
	submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	decision_date DATETIME,
	last_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	enrollment_year_level VARCHAR(20),
	enrollment_semester VARCHAR(20),
	enrollment_student_type VARCHAR(20),
	inventory_gender VARCHAR(255),
	inventory_age VARCHAR(50),
	inventory_religion VARCHAR(255),
	inventory_complete_address TEXT,
	inventory_mobile_number VARCHAR(255),
	inventory_facebook_account TEXT,
	inventory_interest_hobbies TEXT,
	inventory_health_condition TEXT,
	inventory_pre_elementary_school TEXT,
	inventory_pre_elementary_dates VARCHAR(255),
	inventory_pre_elementary_awards TEXT,
	inventory_elementary_school TEXT,
	inventory_elementary_dates VARCHAR(255),
	inventory_elementary_awards TEXT,
	inventory_secondary_school TEXT,
	inventory_secondary_dates VARCHAR(255),
	inventory_secondary_awards TEXT,
	inventory_vocational_school TEXT,
	inventory_vocational_dates VARCHAR(255),
	inventory_vocational_awards TEXT,
	inventory_father_name VARCHAR(255),
	inventory_father_age VARCHAR(50),
	inventory_father_status VARCHAR(255),
	inventory_father_education TEXT,
	inventory_father_occupation TEXT,
	inventory_father_contact VARCHAR(255),
	inventory_mother_name VARCHAR(255),
	inventory_mother_age VARCHAR(50),
	inventory_mother_status VARCHAR(255),
	inventory_mother_education TEXT,
	inventory_mother_occupation TEXT,
	inventory_mother_contact VARCHAR(255),
	inventory_parents_marital_status VARCHAR(255),
	inventory_number_of_children VARCHAR(50),
	inventory_number_of_brothers VARCHAR(50),
	inventory_number_of_sisters VARCHAR(50),
	inventory_guardian_name VARCHAR(255),
	inventory_guardian_relationship TEXT,
	inventory_guardian_contact VARCHAR(255),
	inventory_family_income VARCHAR(255),
	inventory_favorite_colors TEXT,
	inventory_favorite_sports TEXT,
	inventory_favorite_foods TEXT,
	inventory_family_description TEXT,
	inventory_emergency_contact_name VARCHAR(255),
	inventory_emergency_contact_number VARCHAR(255),
	inventory_emergency_contact_relationship VARCHAR(255),
	enrollment_good_moral_file MEDIUMBLOB,
	enrollment_good_moral_filename VARCHAR(255),
	enrollment_good_moral_mimetype VARCHAR(100),
	enrollment_psa_birth_file MEDIUMBLOB,
	enrollment_psa_birth_filename VARCHAR(255),
	enrollment_psa_birth_mimetype VARCHAR(100),
	enrollment_photos_2x2_file MEDIUMBLOB,
	enrollment_photos_2x2_filename VARCHAR(255),
	enrollment_photos_2x2_mimetype VARCHAR(100),
	enrollment_entrance_fee_proof_file MEDIUMBLOB,
	enrollment_entrance_fee_proof_filename VARCHAR(255),
	enrollment_entrance_fee_proof_mimetype VARCHAR(100),
	enrollment_voters_id_file MEDIUMBLOB,
	enrollment_voters_id_filename VARCHAR(255),
	enrollment_voters_id_mimetype VARCHAR(100),
	enrollment_cbs_file MEDIUMBLOB,
	enrollment_cbs_filename VARCHAR(255),
	enrollment_cbs_mimetype VARCHAR(100),
	enrollment_brgy_cert_file MEDIUMBLOB,
	enrollment_brgy_cert_filename VARCHAR(255),
	enrollment_brgy_cert_mimetype VARCHAR(100),
	enrollment_shs_card_file MEDIUMBLOB,
	enrollment_shs_card_filename VARCHAR(255),
	enrollment_shs_card_mimetype VARCHAR(100),
	enrollment_shs_diploma_file MEDIUMBLOB,
	enrollment_shs_diploma_filename VARCHAR(255),
	enrollment_shs_diploma_mimetype VARCHAR(100),
	is_approved_by_president BOOL NOT NULL DEFAULT 0,
	president_approval_date DATETIME,
	is_approved_by_accounting BOOL NOT NULL DEFAULT 0,
	accounting_approval_date DATETIME,
	is_approved_by_dean BOOL NOT NULL DEFAULT 0,
	dean_approval_date DATETIME,
	section_id INTEGER,
	pwd_id_file MEDIUMBLOB,
	pwd_id_filename VARCHAR(255),
	pwd_id_mimetype VARCHAR(100),
	is_section_permanent BOOL DEFAULT 0,
	organization VARCHAR(100),
	PRIMARY KEY (applicant_id),
	FOREIGN KEY(student_user_id) REFERENCES student_users (id),
	UNIQUE (control_number),
	UNIQUE (permit_control_no),
	FOREIGN KEY(section_id) REFERENCES sections (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- faculty_assignments -----------------------------------------
CREATE TABLE IF NOT EXISTS faculty_assignments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	faculty_id INTEGER NOT NULL,
	subject_id INTEGER NOT NULL,
	section_id INTEGER NOT NULL,
	p_max_q1 INTEGER DEFAULT 10,
	p_max_q2 INTEGER DEFAULT 10,
	p_max_q3 INTEGER DEFAULT 0,
	p_max_q4 INTEGER DEFAULT 0,
	p_max_q5 INTEGER DEFAULT 0,
	p_max_r INTEGER DEFAULT 100,
	p_max_act1 INTEGER DEFAULT 50,
	p_max_act2 INTEGER DEFAULT 50,
	p_max_act3 INTEGER DEFAULT 50,
	p_max_act4 INTEGER DEFAULT 0,
	p_max_act5 INTEGER DEFAULT 0,
	p_max_exam INTEGER DEFAULT 100,
	m_max_q1 INTEGER DEFAULT 10,
	m_max_q2 INTEGER DEFAULT 0,
	m_max_q3 INTEGER DEFAULT 0,
	m_max_q4 INTEGER DEFAULT 0,
	m_max_q5 INTEGER DEFAULT 0,
	m_max_r INTEGER DEFAULT 100,
	m_max_act1 INTEGER DEFAULT 50,
	m_max_act2 INTEGER DEFAULT 0,
	m_max_act3 INTEGER DEFAULT 0,
	m_max_act4 INTEGER DEFAULT 0,
	m_max_act5 INTEGER DEFAULT 0,
	m_max_exam INTEGER DEFAULT 100,
	f_max_q1 INTEGER DEFAULT 10,
	f_max_q2 INTEGER DEFAULT 0,
	f_max_q3 INTEGER DEFAULT 0,
	f_max_q4 INTEGER DEFAULT 0,
	f_max_q5 INTEGER DEFAULT 0,
	f_max_r INTEGER DEFAULT 100,
	f_max_act1 INTEGER DEFAULT 50,
	f_max_act2 INTEGER DEFAULT 50,
	f_max_act3 INTEGER DEFAULT 50,
	f_max_act4 INTEGER DEFAULT 0,
	f_max_act5 INTEGER DEFAULT 0,
	f_max_exam INTEGER DEFAULT 100,
	PRIMARY KEY (id),
	FOREIGN KEY(faculty_id) REFERENCES users (id),
	FOREIGN KEY(subject_id) REFERENCES subjects (id),
	FOREIGN KEY(section_id) REFERENCES sections (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- prerequisites -----------------------------------------------
CREATE TABLE IF NOT EXISTS prerequisites (
	id INTEGER NOT NULL AUTO_INCREMENT,
	subject_id INTEGER NOT NULL,
	prerequisite_subject_id INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (subject_id, prerequisite_subject_id),
	FOREIGN KEY(subject_id) REFERENCES subjects (id),
	FOREIGN KEY(prerequisite_subject_id) REFERENCES subjects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_grades ----------------------------------------------
CREATE TABLE IF NOT EXISTS student_grades (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_user_id INTEGER NOT NULL,
	subject_id INTEGER NOT NULL,
	grade NUMERIC(4, 2),
	remarks VARCHAR(20) NOT NULL,
	academic_year VARCHAR(20) NOT NULL,
	semester VARCHAR(50) NOT NULL,
	created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
	updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	percentage INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_student_grades UNIQUE (student_user_id, subject_id, academic_year, semester),
	FOREIGN KEY(student_user_id) REFERENCES student_users (id),
	FOREIGN KEY(subject_id) REFERENCES subjects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_payments --------------------------------------------
CREATE TABLE IF NOT EXISTS student_payments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_user_id INTEGER NOT NULL,
	assessment_id INTEGER,
	receipt_number VARCHAR(60) NOT NULL,
	amount_paid NUMERIC(12, 2) NOT NULL,
	payment_date DATE NOT NULL,
	payment_method VARCHAR(50),
	remark VARCHAR(100),
	cashier_username VARCHAR(80),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(student_user_id) REFERENCES student_users (id),
	FOREIGN KEY(assessment_id) REFERENCES student_assessments (id),
	UNIQUE (receipt_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- subject_prerequisites ---------------------------------------
CREATE TABLE IF NOT EXISTS subject_prerequisites (
	subject_id INTEGER NOT NULL,
	prerequisite_id INTEGER NOT NULL,
	PRIMARY KEY (subject_id, prerequisite_id),
	FOREIGN KEY(subject_id) REFERENCES subjects (id),
	FOREIGN KEY(prerequisite_id) REFERENCES subjects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- teaching_loads ----------------------------------------------
CREATE TABLE IF NOT EXISTS teaching_loads (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	course_code VARCHAR(20) NOT NULL,
	course_description VARCHAR(200),
	section VARCHAR(20) NOT NULL,
	day VARCHAR(20) NOT NULL,
	time_start TIME NOT NULL,
	time_end TIME NOT NULL,
	room VARCHAR(20),
	school_year VARCHAR(9),
	is_published BOOL NOT NULL DEFAULT 1,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	INDEX ix_teaching_loads_school_year (school_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- application_messages ----------------------------------------
CREATE TABLE IF NOT EXISTS application_messages (
	id INTEGER NOT NULL AUTO_INCREMENT,
	applicant_id INTEGER NOT NULL,
	sender_type VARCHAR(20) NOT NULL,
	sender_name VARCHAR(255),
	admin_role VARCHAR(20),
	message_text TEXT NOT NULL,
	is_read BOOL DEFAULT 0,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(applicant_id) REFERENCES applicants (applicant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- assessments -------------------------------------------------
CREATE TABLE IF NOT EXISTS assessments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_id INTEGER NOT NULL,
	description VARCHAR(255) NOT NULL,
	total_amount NUMERIC(10, 2) NOT NULL,
	amount_paid NUMERIC(10, 2) DEFAULT 0.0,
	balance NUMERIC(10, 2) NOT NULL,
	status VARCHAR(20) DEFAULT 'unpaid',
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	batch_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(student_id) REFERENCES applicants (applicant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- grades ------------------------------------------------------
CREATE TABLE IF NOT EXISTS grades (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_id INTEGER NOT NULL,
	subject_id INTEGER NOT NULL,
	faculty_id INTEGER NOT NULL,
	p_q1 FLOAT DEFAULT 0,
	p_q2 FLOAT DEFAULT 0,
	p_q3 FLOAT DEFAULT 0,
	p_q4 FLOAT DEFAULT 0,
	p_q5 FLOAT DEFAULT 0,
	p_act1 FLOAT DEFAULT 0,
	p_act2 FLOAT DEFAULT 0,
	p_act3 FLOAT DEFAULT 0,
	p_act4 FLOAT DEFAULT 0,
	p_act5 FLOAT DEFAULT 0,
	p_recit FLOAT DEFAULT 0,
	p_exam FLOAT DEFAULT 0,
	m_q1 FLOAT DEFAULT 0,
	m_q2 FLOAT DEFAULT 0,
	m_q3 FLOAT DEFAULT 0,
	m_q4 FLOAT DEFAULT 0,
	m_q5 FLOAT DEFAULT 0,
	m_act1 FLOAT DEFAULT 0,
	m_act2 FLOAT DEFAULT 0,
	m_act3 FLOAT DEFAULT 0,
	m_act4 FLOAT DEFAULT 0,
	m_act5 FLOAT DEFAULT 0,
	m_recit FLOAT DEFAULT 0,
	m_exam FLOAT DEFAULT 0,
	f_q1 FLOAT DEFAULT 0,
	f_q2 FLOAT DEFAULT 0,
	f_q3 FLOAT DEFAULT 0,
	f_q4 FLOAT DEFAULT 0,
	f_q5 FLOAT DEFAULT 0,
	f_act1 FLOAT DEFAULT 0,
	f_act2 FLOAT DEFAULT 0,
	f_act3 FLOAT DEFAULT 0,
	f_act4 FLOAT DEFAULT 0,
	f_act5 FLOAT DEFAULT 0,
	f_recit FLOAT DEFAULT 0,
	f_exam FLOAT DEFAULT 0,
	prelim_grade FLOAT,
	midterm_grade FLOAT,
	midterm_standing FLOAT,
	final_term_grade FLOAT,
	final_grade FLOAT,
	final_grade_point FLOAT,
	PRIMARY KEY (id),
	FOREIGN KEY(student_id) REFERENCES applicants (applicant_id),
	FOREIGN KEY(subject_id) REFERENCES subjects (id),
	FOREIGN KEY(faculty_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- student_discounts_applied -----------------------------------
CREATE TABLE IF NOT EXISTS student_discounts_applied (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_id INTEGER NOT NULL,
	org_discount_id INTEGER NOT NULL,
	date_applied DATETIME DEFAULT CURRENT_TIMESTAMP,
	applied_by VARCHAR(100),
	PRIMARY KEY (id),
	FOREIGN KEY(student_id) REFERENCES applicants (applicant_id),
	FOREIGN KEY(org_discount_id) REFERENCES organization_discounts (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- substitution_requests ---------------------------------------
CREATE TABLE IF NOT EXISTS substitution_requests (
	id INTEGER NOT NULL AUTO_INCREMENT,
	teaching_load_id INTEGER NOT NULL,
	original_teacher_id INTEGER NOT NULL,
	substitute_teacher_id INTEGER,
	date_of_absence DATE NOT NULL,
	status VARCHAR(50) NOT NULL DEFAULT 'pending',
	reason TEXT,
	proof_file VARCHAR(255),
	timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(teaching_load_id) REFERENCES teaching_loads (id),
	FOREIGN KEY(original_teacher_id) REFERENCES users (id),
	FOREIGN KEY(substitute_teacher_id) REFERENCES users (id),
	INDEX ix_substitution_requests_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- voucher_returns ---------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_returns (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_id INTEGER NOT NULL,
	school_year VARCHAR(50),
	semester VARCHAR(50),
	returned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	returned_by VARCHAR(100),
	amount NUMERIC(10, 2) DEFAULT 0.0,
	PRIMARY KEY (id),
	FOREIGN KEY(student_id) REFERENCES applicants (applicant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- notifications -----------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	substitution_request_id INTEGER,
	message TEXT NOT NULL,
	is_read BOOL NOT NULL DEFAULT 0,
	timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(substitution_request_id) REFERENCES substitution_requests (id),
	INDEX ix_notifications_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- payments ----------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	student_id INTEGER NOT NULL,
	assessment_id INTEGER,
	amount_paid NUMERIC(10, 2) NOT NULL,
	payment_date DATE NOT NULL,
	payment_method VARCHAR(50) DEFAULT 'Cash',
	remark VARCHAR(100),
	receipt_number VARCHAR(50),
	cashier_username VARCHAR(100),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(student_id) REFERENCES applicants (applicant_id),
	FOREIGN KEY(assessment_id) REFERENCES assessments (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
--  Schema migration ledger (website/schema_migrations.py).
--  A database built from THIS file already contains every change below, so
--  they are recorded as applied. Existing databases: python scripts/migrate.py
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
	id VARCHAR(100) NOT NULL PRIMARY KEY,
	description VARCHAR(500),
	applied_at DATETIME NOT NULL
) DEFAULT CHARSET=utf8mb4;
INSERT IGNORE INTO schema_migrations (id, description, applied_at) VALUES
  ('0001_enrollment_shs_document_columns', 'built into full.sql', NOW()),
  ('0002_document_columns_mediumblob', 'built into full.sql', NOW());

SET FOREIGN_KEY_CHECKS = 1;

-- =====================================================================
--  SEED DATA  (content + switches used by Admin -> Manage Content)
--  Placeholders marked -- EDIT should be changed to real PGPC values.
-- =====================================================================
START TRANSACTION;

-- =========================================================
-- PAGE CONTENT (text fields)
-- =========================================================
INSERT INTO page_content (content_key, content_value) VALUES
-- Contact page / footer contact
('contact_email',        'polytechniccollegepadregarcia@gmail.com'),
('contact_phone',        '(+63) 000 000 0000'),          -- EDIT
('contact_address',      'Padre Garcia, Batangas, Philippines'),
('contact_hours',        'Monday - Friday, 8:00 AM - 5:00 PM'),
('contact_map_url',      'https://maps.app.goo.gl/od3hUBTfTZ1uNNvn7'),

-- Footer
('footer_title',         'Padre Garcia Polytechnic College'),
('footer_subtitle',      'THE FUTURE BEGINS HERE.'),
('footer_connect_text',  'Connect with us'),
('footer_facebook_link', ''),                            -- EDIT: full Facebook URL

-- Consent + Oath
('consent_title', 'CONSENT'),
('consent_text',  'I hereby certify that all information provided in this application is true and correct. I understand that any false statement or omission may disqualify my application and that the school reserves the right to verify the information I submitted.'),
('oath_title',    'OATH'),
('oath_text',     'I promise to abide by the rules and regulations of Padre Garcia Polytechnic College, to uphold its values, and to pursue my studies with honesty, integrity, and dedication.'),

-- Application page sidebar
('sidebar_info_title',    'Important Information'),
('sidebar_info_1',        'Applications are processed on a first-come, first-served basis.'),
('sidebar_info_2',        'Make sure all required documents are complete and valid to avoid delays.'),
('sidebar_info_3',        'Keep your email address active. Updates and announcements are sent through email.'),
('sidebar_contact_title', 'Need Help?'),
('sidebar_phone_label',   'Phone'),
('sidebar_email_label',   'Email'),
('sidebar_email_value',   'polytechniccollegepadregarcia@gmail.com'),
('sidebar_hours_label',   'Office Hours'),
('sidebar_hours_value',   'Monday - Friday, 8:00 AM - 5:00 PM'),

-- Requirements header + status messages
('requirements_header_text', 'Please prepare the following:'),
('status_msg_pending',       'Your application has been received and is awaiting review.'),
('status_msg_review',        'Your application is currently being reviewed by our admissions staff.'),
('status_msg_approved',      'Your application has been approved.'),
('status_msg_scheduled',     'Your entrance exam has been scheduled. Please check your email for details.'),
('status_msg_rejected',      'Your application was not approved. Please contact the admissions office for assistance.'),
('status_msg_passed',        'Congratulations! You passed the entrance exam. Please proceed to enrollment.'),
('status_msg_failed',        'You did not pass the entrance exam. Please contact the admissions office for assistance.'),
('status_msg_enrolling',     'You are now eligible for enrollment. Please complete your enrollment form.'),
('status_msg_enrolled',      'You are officially enrolled. Welcome to PGPC!'),
('status_msg_dropped',       'Your enrollment has been dropped. Please contact the registrar.'),
('status_msg_not_enrolled',  'You are not currently enrolled. Please contact the admissions office.'),

-- About page
('about_hero_title',       'About Padre Garcia Polytechnic College'),
('about_hero_subtitle',    'THE FUTURE BEGINS HERE.'),
('about_hero_type',        'image'),
('about_identity_title',   'Our Identity'),
('about_slogan',           'THE FUTURE BEGINS HERE.'),
('about_vision_text',      'To be a premier polytechnic college that produces competent, values-driven, and globally competitive graduates.'),
('about_mission_text',     'To provide quality, accessible, and relevant technical education that empowers students to become productive citizens and community leaders.'),
('about_core_values',      'Integrity, Excellence, Service, and Commitment'),
('community_title',        'Community Engagement'),
('community_text',         'Padre Garcia Polytechnic College actively partners with the local community through extension programs, outreach activities, and industry linkages that create meaningful opportunities for our students and the people we serve.'),
('president_name',         'President''s Name'),          -- EDIT
('president_role',         'College President'),
('president_desc',         'Leading Padre Garcia Polytechnic College in its mission to provide accessible and quality education for every learner.')

ON DUPLICATE KEY UPDATE content_value = VALUES(content_value);

-- =========================================================
-- SYSTEM SETTINGS (toggles)
-- =========================================================
INSERT INTO system_settings (setting_key, setting_value) VALUES
('admission_open_new',        'true'),
('admission_open_old',        'true'),
('enable_account_creation',   'true'),
('registration_open',         'true'),
('maintenance_mode',          '0'),
('email_trigger_student',     'true'),
('email_trigger_admin',       'true'),
('email_trigger_notes',       'true'),
('email_trigger_announcements','true'),
('issuance_of_receipt',       'true')
ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value);

-- =========================================================
-- SOCIAL MEDIA LINKS (only inserts when the table is empty)
-- =========================================================
INSERT INTO social_media_links (platform_name, url, icon_class)
SELECT 'Facebook', 'https://www.facebook.com/pgpc', 'fab fa-facebook-f'
WHERE NOT EXISTS (SELECT 1 FROM social_media_links);

-- =========================================================
-- STATUS REQUIREMENTS (example rows, only when none exist)
-- =========================================================
INSERT INTO status_requirements (status_key, requirement_text)
SELECT * FROM (
    SELECT 'Pending' AS status_key, 'Valid ID (front and back)' AS requirement_text
    UNION ALL SELECT 'Pending', 'Report of Grades / Transcript of Records'
    UNION ALL SELECT 'Pending', 'Certificate of Good Moral Character'
    UNION ALL SELECT 'Enrolling', 'Original copy of Report of Grades'
    UNION ALL SELECT 'Enrolling', 'Medical Certificate'
) AS defaults
WHERE NOT EXISTS (SELECT 1 FROM status_requirements);

COMMIT;

-- =====================================================================
--  OPTIONAL (uncomment and edit if you want them pre-created)
-- =====================================================================
-- Active school year / semester (the app creates these from the admin UI too):
INSERT INTO academic_terms (year_name, semester, is_active, max_units)
  VALUES ('2025-2026', '1st Semester', 1, 26);
INSERT INTO grading_settings (active_school_year, active_semester)
  VALUES ('2025-2026', '1st Semester');
--
-- Accounts are normally NOT inserted by hand: passwords must be hashed.
-- To pre-create one, generate the hash first:
--   python -c "from werkzeug.security import generate_password_hash as g; print(g('adminpgpc@2025', method='pbkdf2:sha256'))"
-- INSERT INTO users (username, email, password_hash, role, status, full_name, `system`, can_edit)
--   VALUES ('admin', 'admin@example.com', '<PASTE_HASH>', 'admin', 'active', 'System Administrator', 'admission', 1);