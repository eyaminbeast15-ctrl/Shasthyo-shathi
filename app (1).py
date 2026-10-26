"""
Shasthyo Shathi (স্বাস্থ্যসাথী) — telemedicine booking prototype for rural users.

Features
  * Bangla / English interface
  * Find doctors by specialty, see the next free slot, book video / audio / in-person visits
  * Family profiles (book on behalf of parents, children, ...)
  * Health records (prescriptions, lab reports, photos, PDFs) kept in one place
  * Choose exactly which records each doctor may see (per appointment)
  * Video / audio calls through a private Jitsi room link
  * Local SQLite storage, backup download

Run
  pip install streamlit
  streamlit run app.py

NOTE: This is a prototype. Doctors are fictional demo data. A real service needs
authentication, a secure server, doctor verification, encryption and consent logs.
"""
from __future__ import annotations

import secrets
import sqlite3
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

import gateway

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover - tzdata may be missing
    TZ = None

DB_PATH = Path(__file__).with_name("telemed.db")
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_MIME = {"image/png", "image/jpeg", "application/pdf"}
BOOKING_WINDOW_DAYS = 7
JOIN_EARLY_MIN = 10
JOIN_LATE_MIN = 30

# Python weekday numbers (Mon=0 ... Sun=6). Bangladesh work week is Sun-Thu.
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# --------------------------------------------------------------------------
# Static data
# --------------------------------------------------------------------------
SPECIALTIES = {
    "general": {"icon": "🩺", "en": "General physician", "bn": "সাধারণ চিকিৎসক"},
    "child": {"icon": "🧒", "en": "Child specialist", "bn": "শিশু রোগ"},
    "gyn": {"icon": "🤰", "en": "Women's health", "bn": "নারী ও প্রসূতি রোগ"},
    "heart": {"icon": "❤️", "en": "Heart", "bn": "হৃদরোগ"},
    "skin": {"icon": "🧴", "en": "Skin", "bn": "চর্মরোগ"},
    "eye": {"icon": "👁️", "en": "Eye", "bn": "চক্ষু"},
    "ent": {"icon": "👂", "en": "Ear, nose & throat", "bn": "নাক-কান-গলা"},
    "ortho": {"icon": "🦴", "en": "Bones & joints", "bn": "হাড় ও জোড়া"},
    "diabetes": {"icon": "🩸", "en": "Diabetes & hormones", "bn": "ডায়াবেটিস ও হরমোন"},
    "mental": {"icon": "🧠", "en": "Mental health", "bn": "মানসিক স্বাস্থ্য"},
}

MODES = {
    "video": {"icon": "📹", "en": "Video call", "bn": "ভিডিও কল"},
    "audio": {"icon": "📞", "en": "Audio call", "bn": "অডিও কল"},
    "clinic": {"icon": "🏥", "en": "In person", "bn": "সরাসরি চেম্বারে"},
}

RECORD_TYPES = {
    "prescription": {"icon": "💊", "en": "Prescription", "bn": "প্রেসক্রিপশন"},
    "lab": {"icon": "🧪", "en": "Lab report", "bn": "ল্যাব রিপোর্ট"},
    "imaging": {"icon": "🩻", "en": "X-ray / scan", "bn": "এক্স-রে / স্ক্যান"},
    "vitals": {"icon": "📈", "en": "Vitals", "bn": "শারীরিক পরিমাপ"},
    "note": {"icon": "📝", "en": "Note", "bn": "নোট"},
}

RELATIONS = ["self", "mother", "father", "spouse", "son", "daughter", "other"]

# Fictional demo doctors.
DOCTORS = [
    dict(id="d1", en="Dr. Rahima Khatun", bn="ডা. রহিমা খাতুন", spec="general", degree="MBBS", exp=12, fee=300,
         modes=["video", "audio", "clinic"], days=[SUN, MON, TUE, WED, THU, SAT], start="09:00", end="13:00", slot=15),
    dict(id="d2", en="Dr. Anwar Hossain", bn="ডা. আনোয়ার হোসেন", spec="general", degree="MBBS", exp=8, fee=250,
         modes=["video", "audio"], days=[SUN, MON, TUE, WED, THU], start="16:00", end="20:00", slot=15),
    dict(id="d3", en="Dr. Sultana Parvin", bn="ডা. সুলতানা পারভীন", spec="gyn", degree="MBBS, FCPS (Gynae)", exp=15,
         fee=600, modes=["video", "audio", "clinic"], days=[SUN, TUE, THU], start="10:00", end="14:00", slot=20),
    dict(id="d4", en="Dr. Mahbubur Rahman", bn="ডা. মাহবুবুর রহমান", spec="heart", degree="MBBS, MD (Cardiology)",
         exp=20, fee=1000, modes=["video"], days=[MON, WED], start="17:00", end="20:00", slot=20),
    dict(id="d5", en="Dr. Nasrin Akter", bn="ডা. নাসরিন আক্তার", spec="child", degree="MBBS, DCH", exp=10, fee=500,
         modes=["video", "audio"], days=[SUN, MON, TUE, WED, THU, FRI], start="15:00", end="19:00", slot=15),
    dict(id="d6", en="Dr. Faruk Ahmed", bn="ডা. ফারুক আহমেদ", spec="skin", degree="MBBS, DDV", exp=9, fee=400,
         modes=["video", "audio"], days=[SUN, MON, TUE, WED, THU], start="11:00", end="15:00", slot=15),
    dict(id="d7", en="Dr. Sharmin Sultana", bn="ডা. শারমিন সুলতানা", spec="eye", degree="MBBS, DO", exp=11, fee=500,
         modes=["video", "clinic"], days=[MON, TUE, WED], start="09:00", end="12:00", slot=15),
    dict(id="d8", en="Dr. Tanvir Hasan", bn="ডা. তানভীর হাসান", spec="ortho", degree="MBBS, MS (Orthopaedics)",
         exp=13, fee=700, modes=["video"], days=[SUN, TUE, THU], start="18:00", end="21:00", slot=20),
    dict(id="d9", en="Dr. Mita Das", bn="ডা. মিতা দাস", spec="diabetes", degree="MBBS, MD (Endocrinology)", exp=14,
         fee=800, modes=["video", "audio"], days=[SUN, MON, WED], start="10:00", end="13:00", slot=20),
    dict(id="d10", en="Dr. Kamrul Islam", bn="ডা. কামরুল ইসলাম", spec="mental", degree="MBBS, MD (Psychiatry)", exp=16,
         fee=900, modes=["video", "audio"], days=[MON, TUE, WED, THU, SAT], start="16:00", end="20:00", slot=30),
    dict(id="d11", en="Dr. Rubina Yasmin", bn="ডা. রুবিনা ইয়াসমিন", spec="ent", degree="MBBS, DLO", exp=7, fee=450,
         modes=["video", "audio", "clinic"], days=[SUN, MON, TUE, WED, THU], start="10:00", end="13:00", slot=15),
]
DOCTOR_BY_ID = {d["id"]: d for d in DOCTORS}

# --------------------------------------------------------------------------
# Translations
# --------------------------------------------------------------------------
TR = {
    "en": {
        "app_name": "Shasthyo Shathi",
        "tagline": "See a doctor from home",
        "hello": "Welcome, {name}",
        "setup_title": "Let's set up your profile",
        "setup_text": "Add your details once. You can add family members later.",
        "name": "Name",
        "name_req": "Please enter a name.",
        "relation": "Relation",
        "age": "Age",
        "phone": "Phone number",
        "village": "Village / Upazila",
        "save": "Save",
        "saved": "Saved.",
        "nav_home": "🏠 Home",
        "nav_doctors": "🩺 Doctors",
        "nav_appts": "📅 Appointments",
        "nav_records": "📁 Records",
        "nav_family": "👨‍👩‍👧 Family & settings",
        "language": "Language / ভাষা",
        "emergency_title": "🚨 Emergency",
        "emergency_text": "If life is in danger, call 999 now or go to the nearest hospital. Online consultation is not for emergencies.",
        "call_999": "Call 999",
        "call_health": "Health helpline 16263",
        "demo_note": "Prototype: doctors and their details are fictional demo data.",
        "privacy": "Your data is stored only on this computer. This is a prototype — a real service needs a secure server, doctor verification and consent logs.",
        "find_doctor": "Find a doctor",
        "next_appt": "Next appointment",
        "no_upcoming": "No upcoming appointments.",
        "no_upcoming_hint": "Find a doctor to get started.",
        "browse_spec": "Browse by specialty",
        "search": "Search doctor or specialty",
        "all": "All",
        "no_doctors": "No doctors match your search.",
        "exp_years": "{n} years experience",
        "fee": "Fee",
        "next_free": "Next free: {when}",
        "no_free": "No free slots this week",
        "book": "Book",
        "back": "← Back",
        "book_title": "Book appointment",
        "patient": "Patient",
        "consult_mode": "How would you like to consult?",
        "pick_date": "Choose a date",
        "pick_time": "Choose a time",
        "no_slots": "No free slots on this day.",
        "not_working": "The doctor is not available on this day.",
        "reason": "Reason for visit (optional)",
        "reason_ph": "e.g. fever for 3 days",
        "share_title": "Share records with the doctor (optional)",
        "share_hint": "Only the records you select will be visible to this doctor.",
        "no_records_to_share": "This person has no records yet. Add some in the Records page.",
        "confirm": "Confirm booking",
        "fee_line": "Fee: ৳{fee}. You can pay online after booking, or at the consultation.",
        "booked": "Appointment booked!",
        "slot_taken": "That time was just taken. Please pick another.",
        "conflict": "This patient already has an appointment at that time.",
        "upcoming": "Upcoming",
        "past": "Past",
        "no_past": "No past appointments.",
        "join": "Join call",
        "join_wait": "The call opens 10 minutes before the start time.",
        "clinic_note": "Visit in person at your booked time.",
        "shared_n": "{n} record(s) shared",
        "manage_share": "Manage sharing",
        "cancel_appt": "Cancel appointment",
        "cancel_q": "Cancel this appointment?",
        "yes_cancel": "Yes, cancel",
        "keep": "Keep it",
        "st_cancelled": "Cancelled",
        "st_completed": "Completed",
        "for_patient": "Patient: {name}",
        "add_first": "Please add a family member first (Home page).",
        "add_record": "Add a record",
        "rec_type": "Type",
        "title": "Title",
        "title_req": "Please enter a title.",
        "date": "Date",
        "notes": "Notes",
        "attach": "Attach a photo or PDF (up to 5 MB)",
        "file_too_big": "The file is too large (limit 5 MB).",
        "file_bad": "Only PNG, JPG or PDF files are allowed.",
        "no_records": "No records yet. Keep prescriptions, reports and photos in one place.",
        "delete": "Delete",
        "delete_q": "Delete this record?",
        "yes_delete": "Yes, delete",
        "download": "Download file",
        "has_file": "Has attachment",
        "members": "Family members",
        "add_member": "Add a family member",
        "edit": "Edit",
        "rel_self": "Myself",
        "rel_mother": "Mother",
        "rel_father": "Father",
        "rel_spouse": "Spouse",
        "rel_son": "Son",
        "rel_daughter": "Daughter",
        "rel_other": "Other",
        "cant_delete_last": "At least one person must remain.",
        "has_upcoming": "Cancel this person's upcoming appointments first.",
        "delete_member_q": "Delete this person and all their records?",
        "data_title": "Your data",
        "export": "Download backup (.db)",
        "delete_all": "Delete all data",
        "delete_all_q": "Delete all people, records and appointments? This cannot be undone.",
        "confirm_check": "I understand",
        "deleted": "Deleted.",
        "pay_title": "Payment",
        "pay_status_unpaid": "Unpaid",
        "pay_status_pending": "Waiting for payment",
        "pay_status_paid": "Paid",
        "pay_status_failed": "Payment failed",
        "pay_status_refunded": "Refunded",
        "pay_status_refund_due": "Refund due",
        "pay_cash_status": "Pay at consultation",
        "pay_method": "Payment method",
        "m_bkash": "bKash",
        "m_nagad": "Nagad",
        "m_rocket": "Rocket",
        "m_card": "Debit / credit card",
        "m_cash": "Cash at consultation",
        "m_online": "Pay online (bKash, Nagad, Rocket or card)",
        "pay_btn": "Pay {amount}",
        "pay_cash_btn": "I will pay at the consultation",
        "pay_cash_saved": "Okay. Please pay the doctor at the time of consultation.",
        "pay_demo_note": "DEMO: no real money is charged. In the live version you are sent to a secure bKash / Nagad / Rocket / card page.",
        "pay_live_note": "You will be taken to a secure payment page where you can choose bKash, Nagad, Rocket or a card. We never see your PIN or card number.",
        "pay_simulate_fail": "Test a failed payment (demo)",
        "pay_ok": "Payment received. Thank you!",
        "pay_failed_msg": "The payment did not go through. You can try again.",
        "pay_open_gateway": "Open secure payment page",
        "pay_check": "I have paid — check status",
        "pay_still_pending": "Not confirmed yet. Please wait a minute and check again.",
        "pay_abandon": "Cancel this attempt",
        "pay_error": "Could not start the payment: {error}",
        "pay_too_small": "This amount is too small for online payment.",
        "pay_receipt": "Download receipt",
        "pay_refund_demo": "The payment was refunded (demo).",
        "pay_refund_due": "The appointment was cancelled. A refund will be arranged by the clinic.",
        "pay_mode_demo": "Payments: demo (no real money)",
        "pay_mode_live_sandbox": "Payments: SSLCommerz test mode",
        "pay_mode_live": "Payments: live",
        "rc_title": "PAYMENT RECEIPT",
        "rc_doctor": "Doctor",
        "rc_patient": "Patient",
        "rc_when": "Appointment",
        "rc_amount": "Amount",
        "rc_method": "Method",
        "rc_ref": "Reference",
        "rc_paid_at": "Paid at",
        "rc_demo": "DEMO RECEIPT - not a real payment",
    },
    "bn": {
        "app_name": "স্বাস্থ্যসাথী",
        "tagline": "ঘরে বসেই ডাক্তার দেখান",
        "hello": "স্বাগতম, {name}",
        "setup_title": "আপনার প্রোফাইল তৈরি করুন",
        "setup_text": "একবার তথ্য দিন। পরে পরিবারের সদস্য যোগ করতে পারবেন।",
        "name": "নাম",
        "name_req": "নাম লিখুন।",
        "relation": "সম্পর্ক",
        "age": "বয়স",
        "phone": "ফোন নম্বর",
        "village": "গ্রাম / উপজেলা",
        "save": "সংরক্ষণ করুন",
        "saved": "সংরক্ষিত হয়েছে।",
        "nav_home": "🏠 হোম",
        "nav_doctors": "🩺 ডাক্তার",
        "nav_appts": "📅 অ্যাপয়েন্টমেন্ট",
        "nav_records": "📁 রেকর্ড",
        "nav_family": "👨‍👩‍👧 পরিবার ও সেটিংস",
        "language": "Language / ভাষা",
        "emergency_title": "🚨 জরুরি",
        "emergency_text": "জীবনের ঝুঁকি থাকলে এখনই ৯৯৯ নম্বরে কল করুন অথবা নিকটস্থ হাসপাতালে যান। অনলাইন পরামর্শ জরুরি অবস্থার জন্য নয়।",
        "call_999": "৯৯৯ এ কল করুন",
        "call_health": "স্বাস্থ্য বাতায়ন ১৬২৬৩",
        "demo_note": "প্রোটোটাইপ: ডাক্তারদের নাম ও তথ্য কাল্পনিক ডেমো।",
        "privacy": "আপনার তথ্য শুধু এই কম্পিউটারেই সংরক্ষিত থাকে। এটি একটি প্রোটোটাইপ — বাস্তব সেবার জন্য নিরাপদ সার্ভার, ডাক্তারের যাচাই এবং সম্মতির রেকর্ড প্রয়োজন।",
        "find_doctor": "ডাক্তার খুঁজুন",
        "next_appt": "পরবর্তী অ্যাপয়েন্টমেন্ট",
        "no_upcoming": "কোনো আসন্ন অ্যাপয়েন্টমেন্ট নেই।",
        "no_upcoming_hint": "শুরু করতে একজন ডাক্তার খুঁজুন।",
        "browse_spec": "বিভাগ অনুযায়ী দেখুন",
        "search": "ডাক্তার বা বিভাগ খুঁজুন",
        "all": "সব",
        "no_doctors": "কোনো ডাক্তার পাওয়া যায়নি।",
        "exp_years": "{n} বছরের অভিজ্ঞতা",
        "fee": "ফি",
        "next_free": "পরবর্তী খালি সময়: {when}",
        "no_free": "এই সপ্তাহে খালি সময় নেই",
        "book": "বুক করুন",
        "back": "← ফিরে যান",
        "book_title": "অ্যাপয়েন্টমেন্ট বুক করুন",
        "patient": "রোগী",
        "consult_mode": "কীভাবে পরামর্শ নিতে চান?",
        "pick_date": "তারিখ বেছে নিন",
        "pick_time": "সময় বেছে নিন",
        "no_slots": "এই দিনে কোনো খালি সময় নেই।",
        "not_working": "এই দিনে ডাক্তার বসেন না।",
        "reason": "সমস্যার বিবরণ (ঐচ্ছিক)",
        "reason_ph": "যেমন: ৩ দিন ধরে জ্বর",
        "share_title": "ডাক্তারের সাথে রেকর্ড শেয়ার করুন (ঐচ্ছিক)",
        "share_hint": "আপনি যেগুলো বেছে নেবেন, শুধু সেগুলোই এই ডাক্তার দেখতে পারবেন।",
        "no_records_to_share": "এই ব্যক্তির এখনো কোনো রেকর্ড নেই। \"রেকর্ড\" পাতা থেকে যোগ করুন।",
        "confirm": "বুকিং নিশ্চিত করুন",
        "fee_line": "ফি: ৳{fee}। বুকিংয়ের পর অনলাইনে দিতে পারেন, অথবা পরামর্শের সময়।",
        "booked": "অ্যাপয়েন্টমেন্ট বুক হয়েছে!",
        "slot_taken": "এই সময়টি আর খালি নেই। অন্য সময় বেছে নিন।",
        "conflict": "এই রোগীর ওই সময়ে আরেকটি অ্যাপয়েন্টমেন্ট আছে।",
        "upcoming": "আসন্ন",
        "past": "পূর্ববর্তী",
        "no_past": "কোনো পূর্ববর্তী অ্যাপয়েন্টমেন্ট নেই।",
        "join": "কলে যোগ দিন",
        "join_wait": "শুরুর ১০ মিনিট আগে কল চালু হবে।",
        "clinic_note": "নির্ধারিত সময়ে সরাসরি চেম্বারে যান।",
        "shared_n": "{n}টি রেকর্ড শেয়ার করা হয়েছে",
        "manage_share": "শেয়ারিং ঠিক করুন",
        "cancel_appt": "অ্যাপয়েন্টমেন্ট বাতিল",
        "cancel_q": "এই অ্যাপয়েন্টমেন্ট বাতিল করবেন?",
        "yes_cancel": "হ্যাঁ, বাতিল করুন",
        "keep": "রেখে দিন",
        "st_cancelled": "বাতিল",
        "st_completed": "সম্পন্ন",
        "for_patient": "রোগী: {name}",
        "add_first": "আগে একজন সদস্য যোগ করুন (হোম পাতা)।",
        "add_record": "রেকর্ড যোগ করুন",
        "rec_type": "ধরন",
        "title": "শিরোনাম",
        "title_req": "একটি শিরোনাম দিন।",
        "date": "তারিখ",
        "notes": "বিবরণ",
        "attach": "ছবি বা পিডিএফ যুক্ত করুন (সর্বোচ্চ ৫ এমবি)",
        "file_too_big": "ফাইলটি অনেক বড় (সর্বোচ্চ ৫ এমবি)।",
        "file_bad": "শুধু PNG, JPG বা PDF ফাইল যুক্ত করা যাবে।",
        "no_records": "এখনো কোনো রেকর্ড নেই। প্রেসক্রিপশন, রিপোর্ট ও ছবি এক জায়গায় রাখুন।",
        "delete": "মুছুন",
        "delete_q": "এই রেকর্ড মুছে ফেলবেন?",
        "yes_delete": "হ্যাঁ, মুছুন",
        "download": "ফাইল ডাউনলোড",
        "has_file": "ফাইল যুক্ত আছে",
        "members": "পরিবারের সদস্য",
        "add_member": "সদস্য যোগ করুন",
        "edit": "সম্পাদনা",
        "rel_self": "আমি",
        "rel_mother": "মা",
        "rel_father": "বাবা",
        "rel_spouse": "স্বামী/স্ত্রী",
        "rel_son": "ছেলে",
        "rel_daughter": "মেয়ে",
        "rel_other": "অন্যান্য",
        "cant_delete_last": "অন্তত একজন সদস্য থাকতে হবে।",
        "has_upcoming": "আগে এই সদস্যের আসন্ন অ্যাপয়েন্টমেন্ট বাতিল করুন।",
        "delete_member_q": "এই সদস্য এবং তার সব রেকর্ড মুছে ফেলবেন?",
        "data_title": "আপনার তথ্য",
        "export": "ব্যাকআপ ডাউনলোড (.db)",
        "delete_all": "সব তথ্য মুছুন",
        "delete_all_q": "সব সদস্য, রেকর্ড ও অ্যাপয়েন্টমেন্ট মুছে ফেলবেন? এটি ফেরানো যাবে না।",
        "confirm_check": "আমি বুঝেছি",
        "deleted": "মুছে ফেলা হয়েছে।",
        "pay_title": "পেমেন্ট",
        "pay_status_unpaid": "অপরিশোধিত",
        "pay_status_pending": "পেমেন্টের অপেক্ষায়",
        "pay_status_paid": "পরিশোধিত",
        "pay_status_failed": "পেমেন্ট ব্যর্থ",
        "pay_status_refunded": "ফেরত দেওয়া হয়েছে",
        "pay_status_refund_due": "ফেরত বাকি",
        "pay_cash_status": "পরামর্শের সময় দেবেন",
        "pay_method": "পেমেন্টের মাধ্যম",
        "m_bkash": "বিকাশ",
        "m_nagad": "নগদ",
        "m_rocket": "রকেট",
        "m_card": "ডেবিট / ক্রেডিট কার্ড",
        "m_cash": "পরামর্শের সময় নগদ",
        "m_online": "অনলাইনে দিন (বিকাশ, নগদ, রকেট বা কার্ড)",
        "pay_btn": "{amount} পরিশোধ করুন",
        "pay_cash_btn": "পরামর্শের সময় দেব",
        "pay_cash_saved": "ঠিক আছে। পরামর্শের সময় ডাক্তারকে ফি দিন।",
        "pay_demo_note": "ডেমো: আসল টাকা কাটা হবে না। আসল সংস্করণে আপনি নিরাপদ বিকাশ / নগদ / রকেট / কার্ড পাতায় যাবেন।",
        "pay_live_note": "আপনি নিরাপদ পেমেন্ট পাতায় যাবেন, সেখানে বিকাশ, নগদ, রকেট বা কার্ড বেছে নিতে পারবেন। আমরা আপনার পিন বা কার্ড নম্বর দেখতে পাই না।",
        "pay_simulate_fail": "ব্যর্থ পেমেন্ট পরীক্ষা করুন (ডেমো)",
        "pay_ok": "পেমেন্ট পাওয়া গেছে। ধন্যবাদ!",
        "pay_failed_msg": "পেমেন্ট সম্পন্ন হয়নি। আবার চেষ্টা করুন।",
        "pay_open_gateway": "নিরাপদ পেমেন্ট পাতা খুলুন",
        "pay_check": "আমি পেমেন্ট করেছি — অবস্থা দেখুন",
        "pay_still_pending": "এখনো নিশ্চিত হয়নি। এক মিনিট পরে আবার দেখুন।",
        "pay_abandon": "এই চেষ্টা বাতিল করুন",
        "pay_error": "পেমেন্ট শুরু করা যায়নি: {error}",
        "pay_too_small": "অনলাইন পেমেন্টের জন্য এই অঙ্ক খুব কম।",
        "pay_receipt": "রসিদ ডাউনলোড",
        "pay_refund_demo": "পেমেন্ট ফেরত দেওয়া হয়েছে (ডেমো)।",
        "pay_refund_due": "অ্যাপয়েন্টমেন্ট বাতিল হয়েছে। চেম্বার থেকে টাকা ফেরতের ব্যবস্থা করা হবে।",
        "pay_mode_demo": "পেমেন্ট: ডেমো (আসল টাকা নয়)",
        "pay_mode_live_sandbox": "পেমেন্ট: SSLCommerz টেস্ট মোড",
        "pay_mode_live": "পেমেন্ট: লাইভ",
        "rc_title": "পেমেন্ট রসিদ",
        "rc_doctor": "ডাক্তার",
        "rc_patient": "রোগী",
        "rc_when": "অ্যাপয়েন্টমেন্ট",
        "rc_amount": "পরিমাণ",
        "rc_method": "মাধ্যম",
        "rc_ref": "রেফারেন্স",
        "rc_paid_at": "পরিশোধের সময়",
        "rc_demo": "ডেমো রসিদ - আসল পেমেন্ট নয়",
    },
}

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
WEEKDAYS_BN = ["সোমবার", "মঙ্গলবার", "বুধবার", "বৃহস্পতিবার", "শুক্রবার", "শনিবার", "রবিবার"]
MONTHS_BN = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন",
             "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]
WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def lang() -> str:
    return st.session_state.get("lang", "bn")


def t(key: str, **kw) -> str:
    text = TR[lang()].get(key) or TR["en"].get(key) or key
    return text.format(**kw) if kw else text


def num(x) -> str:
    s = str(x)
    return s.translate(BN_DIGITS) if lang() == "bn" else s


def label(table: dict, key: str) -> str:
    return table[key][lang()]


def dname(doc: dict) -> str:
    return doc[lang()]


def fmt_date(d: date) -> str:
    if lang() == "bn":
        return f"{WEEKDAYS_BN[d.weekday()]}, {num(d.day)} {MONTHS_BN[d.month - 1]}"
    return f"{WEEKDAYS_EN[d.weekday()]}, {d.day} {MONTHS_EN[d.month - 1]}"


def fmt_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    h12 = h % 12 or 12
    if lang() == "bn":
        if 4 <= h < 12:
            period = "সকাল"
        elif 12 <= h < 16:
            period = "দুপুর"
        elif 16 <= h < 18:
            period = "বিকাল"
        elif 18 <= h < 20:
            period = "সন্ধ্যা"
        else:
            period = "রাত"
        return f"{period} {num(h12)}:{num(f'{m:02d}')}"
    return f"{h12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'self',
    age INTEGER,
    phone TEXT DEFAULT '',
    village TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    rtype TEXT NOT NULL,
    title TEXT NOT NULL,
    rdate TEXT NOT NULL,
    notes TEXT DEFAULT '',
    filename TEXT,
    mime TEXT,
    data BLOB,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id TEXT NOT NULL,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    start TEXT NOT NULL,
    duration INTEGER NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'booked',
    room TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    PRIMARY KEY (appointment_id, record_id)
);
"""


@st.cache_resource
def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.executescript(gateway.PAYMENT_SCHEMA)
    return con


def q(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in get_db().execute(sql, args).fetchall()]


def run(sql: str, args: tuple = ()) -> int:
    con = get_db()
    cur = con.execute(sql, args)
    con.commit()
    return cur.lastrowid


def get_members() -> list[dict]:
    return q("SELECT * FROM members ORDER BY id")


def get_records(member_id: int | None = None) -> list[dict]:
    sql = ("SELECT id, member_id, rtype, title, rdate, notes, filename, mime, "
           "length(data) AS size, created FROM records")
    args: tuple = ()
    if member_id is not None:
        sql += " WHERE member_id = ?"
        args = (member_id,)
    return q(sql + " ORDER BY rdate DESC, id DESC", args)


def get_appointments() -> list[dict]:
    return q("SELECT a.*, m.name AS member_name FROM appointments a "
             "JOIN members m ON m.id = a.member_id ORDER BY a.start")


# --------------------------------------------------------------------------
# Scheduling logic
# --------------------------------------------------------------------------
def booked_starts(doc_id: str) -> set[str]:
    rows = q("SELECT start FROM appointments WHERE doctor_id = ? AND status != 'cancelled'", (doc_id,))
    return {r["start"] for r in rows}


def slots_for(doc: dict, day: date) -> list[tuple[datetime, bool]]:
    """Return [(start, is_taken)] for a doctor on a day. Past slots are skipped."""
    if day.weekday() not in doc["days"]:
        return []
    fh, fm = map(int, doc["start"].split(":"))
    th, tm = map(int, doc["end"].split(":"))
    minutes, end_minutes = fh * 60 + fm, th * 60 + tm
    mine = booked_starts(doc["id"])
    midnight = datetime.combine(day, datetime.min.time())
    cutoff = now() + timedelta(minutes=20)
    out = []
    index = 0
    while minutes + doc["slot"] <= end_minutes:
        start = midnight + timedelta(minutes=minutes)
        if start > cutoff:
            # Deterministic "busy" pattern so the demo always looks realistic.
            demo_busy = zlib.crc32(f"{doc['id']}{day}{index}".encode()) % 100 < 30
            out.append((start, demo_busy or start.isoformat(timespec="minutes") in mine))
        minutes += doc["slot"]
        index += 1
    return out


def free_slots(doc: dict, day: date) -> list[datetime]:
    return [s for s, taken in slots_for(doc, day) if not taken]


def next_free(doc: dict) -> datetime | None:
    today = now().date()
    for offset in range(BOOKING_WINDOW_DAYS):
        free = free_slots(doc, today + timedelta(days=offset))
        if free:
            return free[0]
    return None


def appt_state(a: dict) -> str:
    if a["status"] == "cancelled":
        return "cancelled"
    end = datetime.fromisoformat(a["start"]) + timedelta(minutes=a["duration"])
    return "completed" if end < now() else "upcoming"


def can_join(a: dict) -> bool:
    start = datetime.fromisoformat(a["start"])
    end = start + timedelta(minutes=a["duration"])
    return start - timedelta(minutes=JOIN_EARLY_MIN) <= now() <= end + timedelta(minutes=JOIN_LATE_MIN)


def call_url(a: dict) -> str:
    url = f"https://meet.jit.si/ShasthyoSathi-{a['room']}"
    return url + "#config.startWithVideoMuted=true" if a["mode"] == "audio" else url


def create_appointment(doc: dict, member_id: int, mode: str, start: datetime, reason: str,
                       shared: list[int]) -> tuple[bool, str, int | None]:
    iso = start.isoformat(timespec="minutes")
    if start not in free_slots(doc, start.date()):
        return False, "slot_taken", None
    clash = q("SELECT 1 FROM appointments WHERE member_id = ? AND start = ? AND status != 'cancelled'",
              (member_id, iso))
    if clash:
        return False, "conflict", None
    appt_id = run(
        "INSERT INTO appointments (doctor_id, member_id, mode, start, duration, reason, room, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc["id"], member_id, mode, iso, doc["slot"], reason.strip(), secrets.token_hex(8),
         now().isoformat(timespec="seconds")),
    )
    set_shares(appt_id, shared)
    gateway.ensure_payment(get_db(), appt_id, doc["fee"])
    return True, "booked", appt_id


def set_shares(appt_id: int, record_ids: list[int]) -> None:
    con = get_db()
    con.execute("DELETE FROM shares WHERE appointment_id = ?", (appt_id,))
    con.executemany("INSERT OR IGNORE INTO shares (appointment_id, record_id) VALUES (?, ?)",
                    [(appt_id, r) for r in record_ids])
    con.commit()


def get_shares(appt_id: int) -> list[int]:
    return [r["record_id"] for r in q("SELECT record_id FROM shares WHERE appointment_id = ?", (appt_id,))]


# --------------------------------------------------------------------------
# Small UI helpers
# --------------------------------------------------------------------------
def set_state(key: str, value) -> None:
    st.session_state[key] = value


def go(page: str) -> None:
    """Navigate from a button callback."""
    st.session_state["page"] = page
    st.session_state["book_doc"] = None


def go_filtered(spec: str) -> None:
    st.session_state["page"] = "doctors"
    st.session_state["spec"] = spec
    st.session_state["book_doc"] = None


def start_booking(doc_id: str) -> None:
    st.session_state["book_doc"] = doc_id


def request_goto(page: str, flash: str | None = None) -> None:
    """Navigate from the page body (widgets already created this run)."""
    st.session_state["_goto"] = page
    if flash:
        st.session_state["_flash"] = flash
    st.rerun()


def member_names() -> dict[int, str]:
    return {m["id"]: m["name"] for m in get_members()}


def record_label(rec: dict) -> str:
    return f"{label(RECORD_TYPES, rec['rtype'])} · {rec['title']} · {rec['rdate']}"


def render_member_form(key: str, member: dict | None = None) -> None:
    with st.form(key, clear_on_submit=member is None):
        name = st.text_input(t("name"), value=member["name"] if member else "", max_chars=80)
        rel_default = RELATIONS.index(member["relation"]) if member else 0
        relation = st.selectbox(t("relation"), RELATIONS, index=rel_default,
                                format_func=lambda r: t(f"rel_{r}"))
        age = st.number_input(t("age"), min_value=0, max_value=120,
                              value=int(member["age"] or 0) if member else 0, step=1)
        phone = st.text_input(t("phone"), value=member["phone"] if member else "", max_chars=20)
        village = st.text_input(t("village"), value=member["village"] if member else "", max_chars=80)
        if st.form_submit_button(t("save"), type="primary"):
            if not name.strip():
                st.error(t("name_req"))
            elif member:
                run("UPDATE members SET name=?, relation=?, age=?, phone=?, village=? WHERE id=?",
                    (name.strip(), relation, int(age), phone.strip(), village.strip(), member["id"]))
                st.session_state["_flash"] = t("saved")
                st.rerun()
            else:
                run("INSERT INTO members (name, relation, age, phone, village) VALUES (?, ?, ?, ?, ?)",
                    (name.strip(), relation, int(age), phone.strip(), village.strip()))
                st.session_state["_flash"] = t("saved")
                st.rerun()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
PAY_ICONS = {"unpaid": "🕒", "pending": "⏳", "paid": "✅", "failed": "❌", "refunded": "↩️", "refund_due": "↩️"}
DEMO_METHODS = ["bkash", "nagad", "rocket", "card"]


def money(amount: float) -> str:
    return f"৳{num(f'{amount:g}')}"


def receipt_text(a: dict, doc: dict, pay: dict) -> str:
    start = datetime.fromisoformat(a["start"])
    paid_at = pay.get("paid_at") or ""
    try:
        parsed = datetime.fromisoformat(paid_at)
        paid_at = (parsed.astimezone(TZ) if TZ else parsed).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    rows = [
        t("app_name"), t("rc_title"), "=" * 34,
        f"{t('rc_doctor')}: {dname(doc)}",
        f"{t('rc_patient')}: {a['member_name']}",
        f"{t('rc_when')}: {fmt_date(start.date())}, {fmt_time(start)}",
        f"{t('rc_amount')}: {money(pay['amount'])}",
        f"{t('rc_method')}: {t('m_' + (pay['method'] or 'online'))}",
        f"{t('rc_ref')}: {pay.get('bank_tran_id') or pay.get('tran_id') or '-'}",
        f"{t('rc_paid_at')}: {paid_at}",
    ]
    if pay.get("provider") == "demo":
        rows += ["", t("rc_demo")]
    return "\n".join(rows) + "\n"


def render_payment(a: dict, doc: dict, prefix: str, state: str) -> None:
    con = get_db()
    pay = gateway.get_payment(con, a["id"])
    if pay is None:  # appointment created before payments existed
        gateway.ensure_payment(con, a["id"], doc["fee"])
        pay = gateway.get_payment(con, a["id"])

    status = pay["status"]
    status_text = t(f"pay_status_{status}")
    if status == "unpaid" and pay["method"] == "cash":
        status_text = t("pay_cash_status")
    method = f" · {t('m_' + pay['method'])}" if status == "paid" and pay["method"] else ""
    st.caption(f"💳 {money(pay['amount'])} · {PAY_ICONS[status]} {status_text}{method}")

    if status == "refunded":
        st.caption(t("pay_refund_demo"))
    elif status == "refund_due":
        st.caption(t("pay_refund_due"))
    elif status == "paid":
        st.download_button("🧾 " + t("pay_receipt"), receipt_text(a, doc, pay), file_name=f"receipt-{a['id']}.txt",
                           mime="text/plain", key=f"rc_{prefix}_{a['id']}")
    elif state != "cancelled":
        render_checkout(a, pay, prefix)


def render_checkout(a: dict, pay: dict, prefix: str) -> None:
    con = get_db()
    live = gateway.mode() == "real"
    key = f"{prefix}_{a['id']}"
    expanded = st.session_state.get("pay_appt") == a["id"] or pay["status"] == "pending"

    def done(message: str, icon: str = "✅") -> None:
        st.session_state["_flash"] = message
        st.session_state["_flash_icon"] = icon
        st.session_state["pay_appt"] = None
        st.rerun()

    with st.expander("💳 " + t("pay_title"), expanded=expanded):
        if live:
            st.info(t("pay_live_note"))
        else:
            st.warning(t("pay_demo_note"))

        if pay["status"] == "pending":
            if pay.get("gateway_url"):
                st.link_button("🔗 " + t("pay_open_gateway"), pay["gateway_url"])
            if st.button(t("pay_check"), key=f"chk_{key}"):
                new = gateway.refresh_payment(con, a["id"]) or pay
                if new["status"] == "paid":
                    done(t("pay_ok"))
                elif new["status"] == "failed":
                    done(t("pay_failed_msg"), "❌")
                else:
                    st.info(t("pay_still_pending"))
            if st.button(t("pay_abandon"), key=f"ab_{key}"):
                gateway.abandon_pending(con, a["id"])
                st.rerun()
            return

        methods = ["online", "cash"] if live else DEMO_METHODS + ["cash"]
        method = st.radio(t("pay_method"), methods, key=f"pm_{key}", format_func=lambda m: t(f"m_{m}"))

        if method == "cash":
            if st.button(t("pay_cash_btn"), key=f"cash_{key}"):
                gateway.choose_cash(con, a["id"])
                done(t("pay_cash_saved"))
            return

        simulate = False if live else st.checkbox(t("pay_simulate_fail"), key=f"sim_{key}")
        if st.button(t("pay_btn", amount=money(pay["amount"])), type="primary", key=f"pay_{key}"):
            member = q("SELECT * FROM members WHERE id = ?", (a["member_id"],))[0]
            customer = {"name": member["name"], "phone": member["phone"], "village": member["village"]}
            res = gateway.start_payment(con, a["id"], method, customer, simulate)
            if res["ok"] and res["status"] == "paid":
                done(t("pay_ok"))
            elif res["ok"]:
                st.rerun()  # now pending: shows the secure payment link
            elif res["status"] == "failed":
                done(t("pay_failed_msg"), "❌")
            elif res.get("error") == "amount_too_small":
                st.error(t("pay_too_small"))
            else:
                st.error(t("pay_error", error=res.get("error", "")))


def render_appointment(a: dict, prefix: str) -> None:
    doc = DOCTOR_BY_ID[a["doctor_id"]]
    start = datetime.fromisoformat(a["start"])
    state = appt_state(a)
    mode = MODES[a["mode"]]
    with st.container(border=True):
        badge = {"upcoming": "", "cancelled": f" · ❌ {t('st_cancelled')}", "completed": f" · ✅ {t('st_completed')}"}[state]
        st.markdown(f"**{dname(doc)}** · {label(SPECIALTIES, doc['spec'])}{badge}")
        st.write(f"📅 {fmt_date(start.date())}  ·  🕒 {fmt_time(start)}")
        st.caption(f"{mode['icon']} {mode[lang()]} · {t('for_patient', name=a['member_name'])}")
        if a["reason"]:
            st.caption(f"💬 {a['reason']}")
        shared_ids = get_shares(a["id"])
        st.caption("🔒 " + t("shared_n", n=num(len(shared_ids))))
        render_payment(a, doc, prefix, state)
        if state != "upcoming":
            return

        if a["mode"] in ("video", "audio"):
            if can_join(a):
                st.markdown(f"### [{mode['icon']} {t('join')}]({call_url(a)})")
            else:
                st.info(t("join_wait"))
        else:
            st.info(t("clinic_note"))

        with st.expander(t("manage_share")):
            options = [r["id"] for r in get_records(a["member_id"])]
            by_id = {r["id"]: r for r in get_records(a["member_id"])}
            if not options:
                st.caption(t("no_records_to_share"))
            else:
                chosen = st.multiselect(t("share_title"), options, default=[i for i in shared_ids if i in by_id],
                                        format_func=lambda i: record_label(by_id[i]), key=f"{prefix}_share_{a['id']}")
                st.caption(t("share_hint"))
                if st.button(t("save"), key=f"{prefix}_saveshare_{a['id']}"):
                    set_shares(a["id"], chosen)
                    st.session_state["_flash"] = t("saved")
                    st.rerun()

        if st.session_state.get("confirm_cancel") == a["id"]:
            st.warning(t("cancel_q"))
            c1, c2 = st.columns(2)
            if c1.button(t("yes_cancel"), key=f"{prefix}_yc_{a['id']}", type="primary"):
                run("UPDATE appointments SET status='cancelled' WHERE id=?", (a["id"],))
                gateway.on_cancel(get_db(), a["id"])
                st.session_state["confirm_cancel"] = None
                st.rerun()
            c2.button(t("keep"), key=f"{prefix}_keep_{a['id']}", on_click=set_state, args=("confirm_cancel", None))
        else:
            st.button(t("cancel_appt"), key=f"{prefix}_cancel_{a['id']}",
                      on_click=set_state, args=("confirm_cancel", a["id"]))


def page_home() -> None:
    members = get_members()
    if not members:
        st.title(f"🩺 {t('app_name')}")
        st.subheader(t("setup_title"))
        st.caption(t("setup_text"))
        render_member_form("first_member")
        return

    st.title(t("hello", name=members[0]["name"]))
    st.subheader(t("tagline"))
    st.button("🩺 " + t("find_doctor"), type="primary", on_click=go, args=("doctors",))

    st.markdown(f"#### {t('next_appt')}")
    upcoming = [a for a in get_appointments() if appt_state(a) == "upcoming"]
    if upcoming:
        render_appointment(upcoming[0], "home")
    else:
        st.info(f"{t('no_upcoming')} {t('no_upcoming_hint')}")

    st.markdown(f"#### {t('browse_spec')}")
    cols = st.columns(2)
    for i, (key, spec) in enumerate(SPECIALTIES.items()):
        cols[i % 2].button(f"{spec['icon']} {spec[lang()]}", key=f"spec_{key}",
                           on_click=go_filtered, args=(key,))
    st.caption(t("demo_note"))


def render_booking(doc: dict) -> None:
    members = get_members()
    st.button(t("back"), on_click=set_state, args=("book_doc", None))
    st.subheader(t("book_title"))
    st.markdown(f"**{dname(doc)}** · {label(SPECIALTIES, doc['spec'])} · {doc['degree']}")

    names = {m["id"]: m["name"] for m in members}
    member_id = st.selectbox(t("patient"), list(names), format_func=lambda i: names[i], key=f"bk_member_{doc['id']}")
    mode = st.radio(t("consult_mode"), doc["modes"], horizontal=True, key=f"bk_mode_{doc['id']}",
                    format_func=lambda m: f"{MODES[m]['icon']} {MODES[m][lang()]}")

    today = now().date()
    day = st.date_input(t("pick_date"), value=today, min_value=today,
                        max_value=today + timedelta(days=BOOKING_WINDOW_DAYS - 1), key=f"bk_date_{doc['id']}")
    st.caption(fmt_date(day))

    slot = None
    if day.weekday() not in doc["days"]:
        st.warning(t("not_working"))
    else:
        free = free_slots(doc, day)
        if free:
            slot = st.selectbox(t("pick_time"), free, format_func=fmt_time, key=f"bk_slot_{doc['id']}_{day}")
        else:
            st.warning(t("no_slots"))

    reason = st.text_area(t("reason"), placeholder=t("reason_ph"), max_chars=240, key=f"bk_reason_{doc['id']}")

    st.markdown(f"**{t('share_title')}**")
    recs = {r["id"]: r for r in get_records(member_id)}
    shared: list[int] = []
    if recs:
        shared = st.multiselect(t("share_title"), list(recs), format_func=lambda i: record_label(recs[i]),
                                key=f"bk_share_{doc['id']}_{member_id}", label_visibility="collapsed")
        st.caption(t("share_hint"))
    else:
        st.caption(t("no_records_to_share"))

    st.info(t("fee_line", fee=num(doc["fee"])))
    if st.button(t("confirm"), type="primary", disabled=slot is None):
        ok, code, new_id = create_appointment(doc, member_id, mode, slot, reason, shared)
        if ok:
            st.session_state["book_doc"] = None
            st.session_state["pay_appt"] = new_id
            request_goto("appts", t("booked"))
        else:
            st.error(t(code))


def page_doctors() -> None:
    if not get_members():
        st.warning(t("add_first"))
        return
    if st.session_state.get("book_doc"):
        render_booking(DOCTOR_BY_ID[st.session_state["book_doc"]])
        return

    st.title("🩺 " + t("find_doctor"))
    text = st.text_input(t("search"), key="q").strip().lower()
    spec = st.selectbox("", ["all"] + list(SPECIALTIES), key="spec", label_visibility="collapsed",
                        format_func=lambda k: t("all") if k == "all" else f"{SPECIALTIES[k]['icon']} {label(SPECIALTIES, k)}")

    shown = 0
    for doc in DOCTORS:
        if spec != "all" and doc["spec"] != spec:
            continue
        haystack = " ".join([doc["en"], doc["bn"], SPECIALTIES[doc["spec"]]["en"], SPECIALTIES[doc["spec"]]["bn"]]).lower()
        if text and text not in haystack:
            continue
        shown += 1
        with st.container(border=True):
            st.markdown(f"**{SPECIALTIES[doc['spec']]['icon']} {dname(doc)}**")
            st.caption(f"{label(SPECIALTIES, doc['spec'])} · {doc['degree']} · {t('exp_years', n=num(doc['exp']))}")
            st.write("  ".join(MODES[m]["icon"] + " " + MODES[m][lang()] for m in doc["modes"]))
            nf = next_free(doc)
            when = t("next_free", when=f"{fmt_date(nf.date())}, {fmt_time(nf)}") if nf else t("no_free")
            st.caption(f"💰 {t('fee')}: ৳{num(doc['fee'])}  ·  🕒 {when}")
            st.button(t("book"), key=f"book_{doc['id']}", type="primary", disabled=nf is None,
                      on_click=start_booking, args=(doc["id"],))
    if not shown:
        st.info(t("no_doctors"))


def page_appointments() -> None:
    if not get_members():
        st.warning(t("add_first"))
        return
    st.title("📅 " + t("nav_appts").split(" ", 1)[1])
    appts = get_appointments()
    upcoming = [a for a in appts if appt_state(a) == "upcoming"]
    past = sorted((a for a in appts if appt_state(a) != "upcoming"), key=lambda a: a["start"], reverse=True)

    tab_up, tab_past = st.tabs([f"{t('upcoming')} ({num(len(upcoming))})", f"{t('past')} ({num(len(past))})"])
    with tab_up:
        if not upcoming:
            st.info(f"{t('no_upcoming')} {t('no_upcoming_hint')}")
            st.button("🩺 " + t("find_doctor"), on_click=go, args=("doctors",), key="appts_find")
        for a in upcoming:
            render_appointment(a, "up")
    with tab_past:
        if not past:
            st.info(t("no_past"))
        for a in past:
            render_appointment(a, "past")


def page_records() -> None:
    members = get_members()
    if not members:
        st.warning(t("add_first"))
        return
    st.title("📁 " + t("nav_records").split(" ", 1)[1])

    names = {m["id"]: m["name"] for m in members}
    member_id = st.selectbox(t("patient"), list(names), format_func=lambda i: names[i], key="rec_member")

    with st.expander("➕ " + t("add_record")):
        with st.form("add_record", clear_on_submit=True):
            rtype = st.selectbox(t("rec_type"), list(RECORD_TYPES),
                                 format_func=lambda k: f"{RECORD_TYPES[k]['icon']} {RECORD_TYPES[k][lang()]}")
            title = st.text_input(t("title"), max_chars=100)
            rdate = st.date_input(t("date"), value=now().date())
            notes = st.text_area(t("notes"), max_chars=1000)
            upload = st.file_uploader(t("attach"), type=["png", "jpg", "jpeg", "pdf"])
            if st.form_submit_button(t("save"), type="primary"):
                if not title.strip():
                    st.error(t("title_req"))
                elif upload is not None and upload.size > MAX_FILE_BYTES:
                    st.error(t("file_too_big"))
                elif upload is not None and upload.type not in ALLOWED_MIME:
                    st.error(t("file_bad"))
                else:
                    run("INSERT INTO records (member_id, rtype, title, rdate, notes, filename, mime, data, created) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (member_id, rtype, title.strip(), rdate.isoformat(), notes.strip(),
                         upload.name if upload else None, upload.type if upload else None,
                         upload.getvalue() if upload else None, now().isoformat(timespec="seconds")))
                    st.session_state["_flash"] = t("saved")
                    st.rerun()

    records = get_records(member_id)
    if not records:
        st.info(t("no_records"))
    for rec in records:
        icon = RECORD_TYPES[rec["rtype"]]["icon"]
        clip = " 📎" if rec["filename"] else ""
        with st.expander(f"{icon} {rec['title']} — {rec['rdate']}{clip}"):
            st.caption(label(RECORD_TYPES, rec["rtype"]))
            if rec["notes"]:
                st.write(rec["notes"])
            if rec["filename"]:
                row = q("SELECT data FROM records WHERE id = ?", (rec["id"],))[0]
                if rec["mime"].startswith("image/"):
                    st.image(row["data"], caption=rec["filename"])
                st.download_button("⬇️ " + t("download"), row["data"], file_name=rec["filename"], mime=rec["mime"],
                                   key=f"dl_{rec['id']}")
            if st.session_state.get("confirm_del") == rec["id"]:
                st.warning(t("delete_q"))
                c1, c2 = st.columns(2)
                if c1.button(t("yes_delete"), key=f"yd_{rec['id']}", type="primary"):
                    run("DELETE FROM records WHERE id = ?", (rec["id"],))
                    st.session_state["confirm_del"] = None
                    st.rerun()
                c2.button(t("keep"), key=f"kd_{rec['id']}", on_click=set_state, args=("confirm_del", None))
            else:
                st.button("🗑️ " + t("delete"), key=f"del_{rec['id']}", on_click=set_state,
                          args=("confirm_del", rec["id"]))


def page_family() -> None:
    st.title(t("nav_family"))
    members = get_members()

    st.subheader(t("members"))
    upcoming_by_member = {m["id"]: 0 for m in members}
    for a in get_appointments():
        if appt_state(a) == "upcoming":
            upcoming_by_member[a["member_id"]] += 1

    for m in members:
        title = f"{m['name']} — {t('rel_' + m['relation'])}"
        with st.expander(title):
            render_member_form(f"edit_{m['id']}", m)
            if st.session_state.get("confirm_del_member") == m["id"]:
                st.warning(t("delete_member_q"))
                c1, c2 = st.columns(2)
                if c1.button(t("yes_delete"), key=f"ydm_{m['id']}", type="primary"):
                    run("DELETE FROM members WHERE id = ?", (m["id"],))
                    st.session_state["confirm_del_member"] = None
                    st.rerun()
                c2.button(t("keep"), key=f"kdm_{m['id']}", on_click=set_state, args=("confirm_del_member", None))
            elif st.button("🗑️ " + t("delete"), key=f"delm_{m['id']}"):
                if len(members) <= 1:
                    st.error(t("cant_delete_last"))
                elif upcoming_by_member[m["id"]]:
                    st.error(t("has_upcoming"))
                else:
                    st.session_state["confirm_del_member"] = m["id"]
                    st.rerun()

    with st.expander("➕ " + t("add_member")):
        render_member_form("add_member")

    st.divider()
    st.subheader(t("data_title"))
    st.caption(t("privacy"))
    if DB_PATH.exists():
        st.download_button("💾 " + t("export"), DB_PATH.read_bytes(), file_name="shasthyo-shathi-backup.db",
                           mime="application/octet-stream")
    with st.expander("⚠️ " + t("delete_all")):
        st.warning(t("delete_all_q"))
        sure = st.checkbox(t("confirm_check"), key="sure_delete_all")
        if st.button(t("delete_all"), disabled=not sure, type="primary"):
            run("DELETE FROM members")
            st.session_state["_flash"] = t("deleted")
            st.rerun()


# --------------------------------------------------------------------------
# App shell
# --------------------------------------------------------------------------
PAGES = {
    "home": page_home,
    "doctors": page_doctors,
    "appts": page_appointments,
    "records": page_records,
    "family": page_family,
}


def main() -> None:
    st.set_page_config(page_title="স্বাস্থ্যসাথী · Shasthyo Shathi", page_icon="🩺", layout="centered")

    for key, default in (("lang", "bn"), ("page", "home"), ("book_doc", None), ("spec", "all"),
                         ("confirm_cancel", None), ("confirm_del", None), ("confirm_del_member", None),
                         ("pay_appt", None)):
        st.session_state.setdefault(key, default)

    # Navigation requested from the page body must be applied before the nav widget exists.
    goto = st.session_state.pop("_goto", None)
    if goto:
        st.session_state["page"] = goto

    with st.sidebar:
        st.markdown(f"## 🩺 {t('app_name')}")
        st.radio(t("language"), ["bn", "en"], key="lang", horizontal=True,
                 format_func=lambda k: "বাংলা" if k == "bn" else "English")
        st.radio("menu", list(PAGES), key="page", label_visibility="collapsed",
                 format_func=lambda k: t(f"nav_{k}"))
        pay_mode = (t("pay_mode_demo") if gateway.mode() == "demo"
                    else t("pay_mode_live_sandbox") if gateway.is_sandbox() else t("pay_mode_live"))
        st.caption("💳 " + pay_mode)
        st.divider()
        st.error(f"**{t('emergency_title')}**\n\n{t('emergency_text')}")
        st.markdown(f"📞 [{t('call_999')}](tel:999)  \n📞 [{t('call_health')}](tel:16263)")

    flash = st.session_state.pop("_flash", None)
    if flash:
        st.toast(flash, icon=st.session_state.pop("_flash_icon", "✅"))

    PAGES[st.session_state["page"]]()


main()
