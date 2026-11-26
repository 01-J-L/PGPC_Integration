-- Seed / reset the content used by Admin -> Manage Content.
-- Safe to re-run: existing keys are updated, missing keys are inserted.
-- Run with:  sudo mysql pgpc_db < seed_manage_content.sql
-- Edit the placeholder values (phone, president, social links, etc.) to match PGPC.

USE pgpc_db;
SET NAMES utf8mb4;

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
